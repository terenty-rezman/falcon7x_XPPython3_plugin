# -*- coding: utf-8 -*-

"""
X-Plane 11
XPPython3 3.1.5
Python 3.10

FMS Web Server

Architecture:

    HTTP thread -> thread-safe request queue -> X-Plane flight loop
                                      <- response queue

IMPORTANT:
    No X-Plane API is called from the HTTP thread.
"""

import json
import os
import queue
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from XPPython3 import xp

# ============================================================
# Configuration
# ============================================================
HOST = "127.0.0.1"
PORT = 8201

# How often to service requests from the HTTP thread.
#
# 0.25 = 4 times per second
# 0.5  = 2 times per second
#
# For an FMS web API 0.25-0.5 is normally more than enough.
FMS_UPDATE_INTERVAL = 0.25
XPLANE_REQUEST_TIMEOUT = 10.0
MAX_HTTP_BODY_SIZE = 64 * 1024


# Requests are created by HTTP worker threads and consumed by the
# X-Plane flight loop.  The response queue belongs to one request only.
class XPlaneRequest:
    def __init__(self, function):
        self.function = function
        self.response = queue.Queue(maxsize=1)


xplane_requests = queue.Queue()
processed_request_count = 0

# ============================================================
# Navigation type
# ============================================================
def nav_type_to_string(nav_type):
    mapping = {
        xp.Nav_Unknown: "unknown",
        xp.Nav_Airport: "airport",
        xp.Nav_NDB: "ndb",
        xp.Nav_VOR: "vor",
        xp.Nav_ILS: "ils",
        xp.Nav_Localizer: "localizer",
        xp.Nav_GlideSlope: "glideslope",
        xp.Nav_OuterMarker: "outer_marker",
        xp.Nav_MiddleMarker: "middle_marker",
        xp.Nav_InnerMarker: "inner_marker",
        xp.Nav_Fix: "fix",
        xp.Nav_DME: "dme",
        xp.Nav_LatLon: "latlon",
    }

    return mapping.get(
        nav_type,
        str(nav_type)
    )

# ============================================================
# Read FMS
#
# THIS FUNCTION MUST ONLY BE CALLED FROM THE
# X-PLANE THREAD.
# ============================================================

def read_fms_from_xplane():
    # Number of entries in the X-Plane FMS.
    count = xp.countFMSEntries()

    # Index of the active destination / active leg.
    destination_index = (
        xp.getDestinationFMSEntry()
    )

    waypoints = []
    for index in range(count):
        info = xp.getFMSEntryInfo(index)
        waypoint = {
            "index": index,
            "id": info.navAidID,
            "type": nav_type_to_string(
                info.type
            ),
            "nav_ref": info.ref,
            "altitude_ft": info.altitude,
            "latitude": info.lat,
            "longitude": info.lon,
            "destination": (
                index == destination_index
            )
        }

        waypoints.append(
            waypoint
        )

    return {
        "valid": True,
        "count": count,
        "destination_index": destination_index,
        "waypoints": waypoints
    }


def fms_directory():
    return os.path.normpath(
        os.path.join(
            xp.getSystemPath(),
            "Output",
            "FMS plans"
        )
    )


def list_fms_files_from_xplane():
    directory = fms_directory()
    if not os.path.isdir(directory):
        return []

    return sorted(
        name for name in os.listdir(directory)
        if name.lower().endswith(".fms")
        and os.path.isfile(os.path.join(directory, name))
    )


def get_fms_files_from_xplane():
    return {"files": list_fms_files_from_xplane()}


def parse_fms_file(path):
    entries = []
    with open(path, "r", encoding="utf-8-sig") as fms_file:
        for line in fms_file:
            fields = line.strip().split()
            if len(fields) < 8 or not fields[0].isdigit():
                continue

            try:
                # Standard X-Plane .fms entries end with identifier,
                # latitude, longitude and altitude.
                entries.append({
                    "latitude": float(fields[-3]),
                    "longitude": float(fields[-2]),
                    "altitude": int(float(fields[-1]))
                })
            except ValueError:
                continue

    if not entries:
        raise ValueError("The FMS file contains no valid entries")
    return entries


def load_fms_file_from_xplane(filename):
    if os.path.basename(filename) != filename:
        raise ValueError("Invalid FMS filename")

    path = os.path.realpath(os.path.join(fms_directory(), filename))
    directory = os.path.realpath(fms_directory())
    if os.path.commonpath([path, directory]) != directory:
        raise ValueError("Invalid FMS filename")
    if not os.path.isfile(path) or not filename.lower().endswith(".fms"):
        raise FileNotFoundError(filename)

    with open(path, "rb") as fms_file:
        plan_data = fms_file.read()

    load_fms = getattr(xp, "loadFMSFlightPlan", None)
    if load_fms is not None:
        load_fms(0, plan_data, len(plan_data))
        return {
            "loaded": True,
            "file": filename
        }

    # Older XPPython3 builds may not expose XPLMLoadFMSFlightPlan.
    # Keep a limited lat/lon fallback for those installations.
    entries = parse_fms_file(path)
    current_count = xp.countFMSEntries()
    for index in range(current_count - 1, -1, -1):
        xp.clearFMSEntry(index)

    for index, entry in enumerate(entries):
        xp.setFMSEntryLatLon(
            index,
            entry["latitude"],
            entry["longitude"],
            entry["altitude"]
        )

    return {
        "loaded": True,
        "file": filename,
        "count": len(entries)
    }


def submit_xplane_request(function):
    request = XPlaneRequest(function)
    try:
        xplane_requests.put(request, timeout=XPLANE_REQUEST_TIMEOUT)
    except queue.Full:
        raise RuntimeError("X-Plane request queue is full")

    try:
        result = request.response.get(timeout=XPLANE_REQUEST_TIMEOUT)
    except queue.Empty:
        raise TimeoutError("X-Plane did not process the request in time")

    if isinstance(result, Exception):
        raise result
    return result


# ============================================================
# X-Plane flight loop
# ============================================================
def fms_update_callback(
        since_last,
        elapsed_time,
        counter,
        refcon):

    global processed_request_count
    while True:
        try:
            request = xplane_requests.get_nowait()
        except queue.Empty:
            break

        try:
            request.response.put(request.function())
        except Exception as e:
            request.response.put(e)
            xp.log(
                "[FMS Web Server] "
                "X-Plane request error: {}".format(e)
            )
        processed_request_count += 1

    return FMS_UPDATE_INTERVAL


# ============================================================
# HTTP Request Handler
# ============================================================
class FMSRequestHandler(BaseHTTPRequestHandler):
    # Disable standard HTTP logging.
    # Otherwise every GET request will spam XPPython3Log.txt.
    def log_message(self, format, *args):
        pass

    # --------------------------------------------------------
    # JSON response
    # --------------------------------------------------------
    def send_json(self, data, status=200):
        body = json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ).encode("utf-8")

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header(
            "Content-Length",
            str(len(body))
        )

        # Useful if JavaScript running in a browser
        # accesses the API.
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )
        self.send_header(
            "Cache-Control",
            "no-cache, no-store"
        )
        self.end_headers()

        self.wfile.write(
            body
        )

    def request_from_xplane(self, function):
        try:
            return submit_xplane_request(function)
        except FileNotFoundError:
            self.send_json({"error": "fms_file_not_found"}, status=404)
        except (ValueError, TypeError) as e:
            self.send_json({"error": "invalid_request", "message": str(e)},
                           status=400)
        except (RuntimeError, TimeoutError) as e:
            self.send_json({"error": "xplane_unavailable", "message": str(e)},
                           status=503)
        return None

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(
            self.path
        )
        path = parsed.path

        # ----------------------------------------------------
        # /
        # ----------------------------------------------------
        if path == "/":
            self.send_json({
                "service":
                    "X-Plane FMS Web Server",

                "version":
                    "1.0",

                "xppython3":
                    "3.1.5",

                "endpoints": [
                    "GET /",
                    "GET /health",
                    "GET /fms",
                    "GET /fms/files",
                    "POST /fms/load"
                ]
            })
            return

        # ----------------------------------------------------
        # /health
        # ----------------------------------------------------
        if path == "/health":
            self.send_json({
                "status": "ok",
                "cache_version":
                    processed_request_count,
                "pending_requests":
                    xplane_requests.qsize(),
                "processed_requests":
                    processed_request_count
            })
            return

        # ----------------------------------------------------
        # /fms
        # ----------------------------------------------------
        if path == "/fms":
            data = self.request_from_xplane(read_fms_from_xplane)
            if data is not None:
                self.send_json(data)
            return

        if path == "/fms/files":
            data = self.request_from_xplane(
                get_fms_files_from_xplane
            )
            if data is not None:
                self.send_json(data)
            return

        # ----------------------------------------------------
        # Not found
        # ----------------------------------------------------
        self.send_json(
            {
                "error":
                    "not_found"
            },
            status=404
        )

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/fms/load":
            self.send_json({"error": "not_found"}, status=404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json({"error": "invalid_content_length"}, status=400)
            return
        if content_length <= 0 or content_length > MAX_HTTP_BODY_SIZE:
            self.send_json({"error": "invalid_body_size"}, status=400)
            return

        try:
            body = self.rfile.read(content_length)
            payload = json.loads(body.decode("utf-8"))
            filename = payload["file"]
            if not isinstance(filename, str):
                raise TypeError("file must be a string")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            self.send_json({"error": "invalid_json"}, status=400)
            return

        result = self.request_from_xplane(
            partial(load_fms_file_from_xplane, filename)
        )
        if result is not None:
            self.send_json(result)

# ============================================================
# HTTP server
# ============================================================

class FMSHTTPServer(ThreadingHTTPServer):
    # Allow quick restart after X-Plane/plugin reload.
    allow_reuse_address = True

http_server = None
http_thread = None


# ============================================================
# Start HTTP server
# ============================================================

def start_http_server():
    global http_server
    global http_thread

    http_server = FMSHTTPServer(
        (
            HOST,
            PORT
        ),
        FMSRequestHandler
    )

    # daemon=True means Python will not keep the process
    # alive because of this thread during shutdown.
    http_thread = threading.Thread(
        target=http_server.serve_forever,
        name="FMS-HTTP-Server",
        daemon=True
    )

    http_thread.start()
    xp.log(
        "[FMS Web Server] "
        "HTTP server started at "
        "http://{}:{}".format(
            HOST,
            PORT
        )
    )


# ============================================================
# Stop HTTP server
# ============================================================

def stop_http_server():
    global http_server
    global http_thread

    if http_server is None:

        return

    xp.log(
        "[FMS Web Server] "
        "Stopping HTTP server..."
    )

    try:
        # Stop serve_forever().
        http_server.shutdown()

    except Exception as e:
        xp.log(
            "[FMS Web Server] "
            "HTTP shutdown error: {}".format(e)
        )

    try:
        http_server.server_close()
    except Exception as e:
        xp.log(
            "[FMS Web Server] "
            "HTTP close error: {}".format(e)
        )

    if http_thread is not None:
        http_thread.join(
            timeout=2.0
        )

    http_thread = None
    http_server = None

    xp.log(
        "[FMS Web Server] "
        "HTTP server stopped"
    )


# ============================================================
# XPPython3 plugin API
# ============================================================

flight_loop_registered = False


class PythonInterface:
    def XPluginStart(self):
        global flight_loop_registered

        name = "FMS Web Server"
        signature = "com.xplane.fms_webserver"
        description = (
            "HTTP API exposing "
            "X-Plane FMS"
        )
        xp.log(
            "[FMS Web Server] "
            "Starting"
        )

        # --------------------------------------------------------
        # Register flight loop
        # --------------------------------------------------------
        xp.registerFlightLoopCallback(
            fms_update_callback,
            interval=FMS_UPDATE_INTERVAL
        )

        flight_loop_registered = True
        # --------------------------------------------------------
        # Start HTTP server
        #
        # --------------------------------------------------------
        try:
            start_http_server()
        except Exception as e:
            xp.log(
                "[FMS Web Server] "
                "HTTP server start failed: {}".format(e)
            )

        return (
            name,
            signature,
            description
        )

    # ============================================================
    # Stop plugin
    # ============================================================
    def XPluginStop(self):
        global flight_loop_registered
        xp.log(
            "[FMS Web Server] "
            "Stopping"
        )
        # --------------------------------------------------------
        # Stop HTTP first
        # --------------------------------------------------------
        stop_http_server()

        # --------------------------------------------------------
        # Then unregister X-Plane callback
        # --------------------------------------------------------
        if flight_loop_registered:
            try:
                xp.unregisterFlightLoopCallback(
                    fms_update_callback
                )
            except Exception as e:
                xp.log(
                    "[FMS Web Server] "
                    "Flight loop unregister error: {}".format(
                        e
                    )
                )
            flight_loop_registered = False

        xp.log(
            "[FMS Web Server] "
            "Stopped"
        )


    # ============================================================
    # Enable
    # ============================================================
    def XPluginEnable(self):
        return 1


    # ============================================================
    # Disable
    # ============================================================
    def XPluginDisable(self):
        pass


    # ============================================================
    # Plugin messages
    # ============================================================
    def XPluginReceiveMessage(
            self,
            inFromWho,
            inMessage,
            inParam):
        pass
