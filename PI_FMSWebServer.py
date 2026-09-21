# -*- coding: utf-8 -*-

"""
X-Plane 11
XPPython3 3.1.5
Python 3.10

FMS Web Server

Architecture:

    HTTP thread
        |
        | GET /fms
        v
    thread-safe cache
        ^
        |
        | update
        |
    X-Plane flight loop
        |
        v
    XPPython3 / X-Plane API

IMPORTANT:
    No X-Plane API is called from the HTTP thread.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from XPPython3 import xp

# ============================================================
# Configuration
# ============================================================
HOST = "127.0.0.1"
PORT = 8201

# How often to update the FMS cache.
#
# 0.25 = 4 times per second
# 0.5  = 2 times per second
#
# For an FMS web API 0.25-0.5 is normally more than enough.
FMS_UPDATE_INTERVAL = 0.25


# ============================================================
# Shared FMS cache
# ============================================================

class FMSCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {
            "valid": False,
            "count": 0,
            "destination_index": -1,
            "waypoints": []
        }
        self._version = 0

    def update(self, data):
        # Make sure the HTTP thread never sees a partially
        # modified Python object.
        with self._lock:
            self._data = data
            self._version += 1

    def get(self):
        with self._lock:
            # JSON-compatible structures consisting of
            # dict/list/int/float/string/bool/None are safe
            # to copy this way.
            return json.loads(
                json.dumps(
                    self._data,
                    ensure_ascii=False
                )
            )

    def version(self):
        with self._lock:
            return self._version


fms_cache = FMSCache()

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


# ============================================================
# X-Plane flight loop
# ============================================================
def fms_update_callback(
        since_last,
        elapsed_time,
        counter,
        refcon):

    try:
        # IMPORTANT:
        #
        # This code executes from X-Plane's flight loop.
        #
        # Therefore it is the ONLY place where we access
        # the X-Plane FMS API.
        data = read_fms_from_xplane()

        # After X-Plane API calls are finished we put only
        # normal Python data into the shared cache.
        fms_cache.update(data)

    except Exception as e:
        xp.log(
            "[FMS Web Server] "
            "FMS update error: {}".format(e)
        )

    # Call again after FMS_UPDATE_INTERVAL seconds.
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
                    "GET /fms"
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
                    fms_cache.version()
            })
            return

        # ----------------------------------------------------
        # /fms
        # ----------------------------------------------------
        if path == "/fms":
            # IMPORTANT:
            #
            # There are NO xp.* calls here.
            #
            # HTTP thread only reads the Python cache.
            data = fms_cache.get()
            self.send_json(
                data
            )
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
        # Initial FMS snapshot
        #
        # XPluginStart executes in the X-Plane plugin context,
        # so it is safe to initialize the cache here.
        # --------------------------------------------------------
        try:
            initial_fms = (
                read_fms_from_xplane()
            )
            fms_cache.update(
                initial_fms
            )
        except Exception as e:
            xp.log(
                "[FMS Web Server] "
                "Initial FMS read failed: {}".format(e)
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
        # IMPORTANT:
        #
        # No X-Plane API calls are made from the HTTP thread.
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
