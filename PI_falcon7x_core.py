from XPPython3 import xp
import requests


CORE_ADDRESS = "127.0.0.1"
CORE_PORT = 6070

SHOW_WINDOW = False


# add callbacks for following commands
commands_for_callbacks = {
    "sim/operation/reload_aircraft_no_art",
    "sim/operation/reload_aircraft"
}


class PythonInterface:
    def XPluginStart(self):
        self.Name = "falcon7x core bridge"
        self.Sig = "falcon7x_core_bridge.xppython3"
        self.Desc = "falcon7x core bridge"

        if SHOW_WINDOW:
            self.WindowId = xp.createWindowEx(
                50, 600, 300, 400, 1,
                self.DrawWindowCallback,
                self.MouseClickCallback,
                self.KeyCallback,
                self.CursorCallback,
                self.MouseWheelCallback,
                0,
                xp.WindowDecorationRoundRectangle,
                xp.WindowLayerFloatingWindows,
                None
            )

        self.counter = 0
        self.msg = "no callbacks called"

        self.command_refs = {}

        for command in commands_for_callbacks:
            cmd_ref = xp.findCommand(command)
            self.command_refs[cmd_ref] = command

            xp.registerCommandHandler(cmd_ref, self.command_callback)

        return self.Name, self.Sig, self.Desc
    
    def command_callback(self, _cmdRef, phase, _refCon):
        # callback on command end
        if phase == 2:
            self.counter += 1

            command_dataref = self.command_refs[_cmdRef]
            requests.post(
                f'http://{CORE_ADDRESS}:{CORE_PORT}' + "/api/command_callback", 
                json={"command": command_dataref},
                timeout=0.1
            )

        self.msg = f"{self.counter} callbacks called"

    def XPluginStop(self):
        for cmd_ref, command in self.command_refs.items():
            xp.unregisterCommandHandler(cmd_ref, self.command_callback)

        if SHOW_WINDOW:
            xp.destroyWindow(self.WindowId)

    def XPluginEnable(self):
        return 1

    def XPluginDisable(self):
        pass

    def XPluginReceiveMessage(self, inFromWho, inMessage, inParam):
        pass

    def DrawWindowCallback(self, inWindowID, inRefcon):
        (left, top, right, bottom) = xp.getWindowGeometry(inWindowID)

        xp.drawTranslucentDarkBox(left, top, right, bottom)
        color = 1.0, 1.0, 1.0

        xp.drawString(color, left + 5, top - 20, self.msg, 0, xp.Font_Basic)

    def KeyCallback(self, inWindowID, inKey, inFlags, inVirtualKey, inRefcon, losingFocus):
        pass

    def MouseClickCallback(self, inWindowID, x, y, inMouse, inRefcon):
        return 1

    def CursorCallback(self, inWindowID, x, y, inRefcon):
        return xp.CursorDefault

    def MouseWheelCallback(self, inWindowID, x, y, wheel, clicks, inRefcon):
        return 1
