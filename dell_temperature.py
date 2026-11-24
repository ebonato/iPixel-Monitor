import logging
import sys


LOGGER = logging.getLogger("ipixel.dell")
THERMAL_METHOD = "Thermal_Information"
TEMPERATURE_OPERATION = 4


class DellTemperature:
    """Read a Dell firmware temperature through the native Windows WMI provider."""

    def __init__(self, sensor_id):
        # pythoncom defaults to STA, which blocks Bleak's WinRT callbacks on the main thread.
        sys.coinit_flags = 0
        try:
            import pythoncom
            from pywintypes import com_error
            from win32com.client.dynamic import Dispatch
        except ImportError as error:
            raise ValueError("Instale pywin32 com: python -m pip install -r requirements-dell.txt") from error

        self.pythoncom = pythoncom
        self.com_error = com_error
        self.dispatch = Dispatch
        self.argument = (sensor_id << 8) | TEMPERATURE_OPERATION
        self.instance_path = None
        self.last_error = None

    def read(self):
        # The monitor collects metrics on worker threads, each with its own COM apartment.
        self.pythoncom.CoInitialize()
        try:
            temperature = self.read_wmi()
        except (self.com_error, OSError, ValueError) as error:
            reason = str(error)
            if reason != self.last_error:
                LOGGER.warning("Sensor Dell indisponível (%s). Confira o sensor e o acesso de administrador.", reason)
            self.last_error = reason
            self.instance_path = None
            return None
        finally:
            self.pythoncom.CoUninitialize()
        self.last_error = None
        return temperature

    def read_wmi(self):
        locator = self.dispatch("WbemScripting.SWbemLocator")
        services = locator.ConnectServer(".", r"root\WMI")
        if self.instance_path is None:
            sensor = next(iter(services.InstancesOf("AWCCWmiMethodFunction")), None)
            if sensor is None:
                raise ValueError("Interface térmica Dell ausente")
            self.instance_path = sensor.Path_.RelPath
        else:
            sensor = services.Get(self.instance_path)

        arguments = sensor.Methods_.Item(THERMAL_METHOD).InParameters.SpawnInstance_()
        arguments.Properties_.Item("arg2").Value = self.argument
        result = sensor.ExecMethod_(THERMAL_METHOD, arguments)
        temperature = int(result.Properties_.Item("argr").Value)
        if not 0 < temperature <= 150:
            raise ValueError("O firmware não retornou uma temperatura válida")
        return temperature
