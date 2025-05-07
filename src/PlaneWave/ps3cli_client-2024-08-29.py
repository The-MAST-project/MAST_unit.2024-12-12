from multiprocessing import shared_memory
import json
import socket
import time


class PS3AutofocusResult:
    has_solution: bool
    best_focus_position: int

    def __init__(self, d: dict):
        self.has_solution = d['has_solution']
        self.best_focus_position = d['best_focus_position']


class PS3CLIClient:
    def __init__(self):
        self.sock = None
        self.log_exchanges = True

    def connect(self, host, port):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((host, port))
        except Exception as e:
            raise Exception(f"Failed to connect to {host}:{port}")

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def platesolve_status(self):
        return self.send_receive("platesolve_status")

    def begin_platesolve_file(self,
                              image_file_path,
                              arcsec_per_pixel_guess,
                              enable_all_sky_match=None,
                              enable_local_quad_match=None,
                              enable_local_triangle_match=None,
                              ra_guess_j2000_rads=None,
                              dec_guess_j2000_rads=None
                              ):
        params = {
            "arcsec_per_pixel": arcsec_per_pixel_guess,
            "image_file_path": image_file_path,
        }

        if enable_all_sky_match is not None:
            params["enable_all_sky_match"] = enable_all_sky_match
        if enable_local_quad_match is not None:
            params["enable_local_quad_match"] = enable_local_quad_match
        if enable_local_triangle_match is not None:
            params["enable_local_triangle_match"] = enable_local_triangle_match
        if ra_guess_j2000_rads is not None:
            params["ra_guess_j2000_rads"] = ra_guess_j2000_rads
        if dec_guess_j2000_rads is not None:
            params["dec_guess_j2000_rads"] = dec_guess_j2000_rads

        return self.send_receive("begin_platesolve", params)

    def begin_platesolve_shm(self,
                             shm_key,
                             width_pixels,
                             height_pixels,
                             arcsec_per_pixel_guess,
                             # Optional params; included in request message only if not None
                             enable_all_sky_match=None,
                             enable_local_quad_match=None,
                             enable_local_triangle_match=None,
                             ra_guess_j2000_rads=None,
                             dec_guess_j2000_rads=None
                             ):

        params = {
            "arcsec_per_pixel": arcsec_per_pixel_guess,
            "shm_image": {
                "shm_key": shm_key,
                "width_pixels": width_pixels,
                "height_pixels": height_pixels
            },
        }
        if enable_all_sky_match is not None:
            params["enable_all_sky_match"] = enable_all_sky_match
        if enable_local_quad_match is not None:
            params["enable_local_quad_match"] = enable_local_quad_match
        if enable_local_triangle_match is not None:
            params["enable_local_triangle_match"] = enable_local_triangle_match
        if ra_guess_j2000_rads is not None:
            params["ra_guess_j2000_rads"] = ra_guess_j2000_rads
        if dec_guess_j2000_rads is not None:
            params["dec_guess_j2000_rads"] = dec_guess_j2000_rads

        return self.send_receive("begin_platesolve", params)

    def platesolve_cancel(self):
        return self.send_receive("platesolve_cancel")

    def analyze_focus(self, file_list: list[str]):
        params = {
            "files": file_list
        }

        # Note: This method currently blocks until the focus analysis has finished.
        # It may change to an asynchronous method in a future version.
        return self.send_receive("analyze_focus", params)

    ##### Low-level methods #####

    def send_request(self, method, params=None):
        self._check_connected()

        request_dict = {"method": method}
        if params is not None:
            request_dict["params"] = params

        request = json.dumps(request_dict).strip()
        if self.log_exchanges:
            print("SEND:", request)
        # Send the message followed by a blank line
        self.sock.sendall((request + "\r\n\r\n").encode('utf-8'))

    def receive_response(self):
        # Read data from the socket until a blank line is received
        data = ""
        while True:
            chunk = self.sock.recv(4096).decode('utf-8')
            data += chunk
            if "\r\n\r\n" in data:
                break
        # Remove the trailing blank line
        data = data.strip()
        if self.log_exchanges:
            print("RECV:", data)
        # Parse the JSON response
        response = json.loads(data)
        return response

    def send_receive(self, method, params=None):
        self.send_request(method, params)
        response = self.receive_response()
        return self._get_result_or_raise_error(response)

    def _get_result_or_raise_error(self, response):
        if "error" in response:
            raise Exception("Received error in response: " + repr(response["error"]))
        elif "result" in response:
            return response["result"]
        else:
            return None

    def _check_connected(self):
        if self.sock is None:
            raise Exception("Not connected")


###### Sample methods for using the PS3 client ######

def test_ascom_status(ps3: PS3CLIClient):
    status = ps3.platesolve_status()
    print(status)


def test_platesolve_file(ps3: PS3CLIClient):
    filename = input("FITS filename: ")
    arcsec_per_pixel_guess = input("Arcsec per pixel (guess): ")
    arcsec_per_pixel_guess = float(arcsec_per_pixel_guess)

    ps3.begin_platesolve_file(
        filename,
        arcsec_per_pixel_guess
    )

    monitor_solve_status(ps3)


def test_ascom_camera_shm(ps3: PS3CLIClient):
    from win32com.client import Dispatch
    import numpy as np

    prog_id = input("Enter ProgID of ASCOM camera driver: ")
    camera = Dispatch(prog_id)
    camera.Connected = True

    arcsec_per_pixel_guess = input("Arcsec per pixel (guess): ")
    arcsec_per_pixel_guess = float(arcsec_per_pixel_guess)

    shm_size = camera.NumX * camera.NumY * 2
    image_shm = shared_memory.SharedMemory(create=True, size=shm_size)
    print(f"Shared memory name: {image_shm.name}")

    while True:
        input("Press Enter to begin an exposure, or Ctrl-C to exit")
        print("Exposing...")
        exp_length_sec = 1
        camera.StartExposure(exp_length_sec, True)
        while not camera.ImageReady:
            time.sleep(0.1)
        t0 = time.time()
        print("Reading image array")
        image_array = camera.ImageArray
        t1 = time.time() - t0
        print(t1, "sec")
        print("Copying to shm")
        shared_image = np.ndarray((camera.NumX, camera.NumY), dtype=np.uint16, buffer=image_shm.buf)
        shared_image[:] = image_array[:]
        print(time.time() - t0, "sec")

        result = ps3.begin_platesolve_shm(image_shm.name, camera.NumX, camera.NumY, arcsec_per_pixel_guess)
        print("Result:", result)

        monitor_solve_status(ps3)


def monitor_solve_status(ps3):
    while True:
        status = ps3.platesolve_status()
        indented_status = json.dumps(status, indent=4)
        print(indented_status)
        if status["state"] == "found_match":
            print("Found match")
            break
        elif status["state"] == "error":
            print("Error during solve")
            break
        elif status["state"] == "no_match":
            print("Match not found")
            break
        time.sleep(0.1)


def test_autofocus(ps3: PS3CLIClient):
    import glob

    focus_dir = input("Enter path to a directory containing FITS images matching the name 'FOCUSnnnnn.fits': ")

    focus_images = glob.glob(focus_dir + "/FOCUS*.f*t*")  # match .fit, .fits, and .fts

    print("Analyzing images:")
    for image in focus_images:
        print("  ", image)

    result = ps3.analyze_focus(focus_images)
    print("Result:")
    print(result)


def test_bad_method(ps3: PS3CLIClient):
    try:
        response = ps3.send_receive("bogus_method")
        print("Response:", response)
    except Exception as e:
        print("Caught exception:")
        print(e)


def main():
    print("Connecting to PlateSolve server")
    ps3 = PS3CLIClient()
    ps3.connect("127.0.0.1", 9897)

    while True:
        print("PS3 Client Tester options")
        print("0: Enable logging of sent/received messages to console")
        print("1: Display PlateSolve server status")
        print("2: PlateSolve a file")
        print("3: PlateSolve an image from an ASCOM camera via shared memory")
        print("4: Analyze focus images")
        print("5: Intentionally send a bad request to check for an error response")
        print("6: Close connection and exit")

        option = input("Selection: ").strip()

        if option == "0":
            ps3.log_exchanges = True
            print("Enabled logging of exchanged messages")
        elif option == "1":
            test_ascom_status(ps3)
        elif option == "2":
            test_platesolve_file(ps3)
        elif option == "3":
            test_ascom_camera_shm(ps3)
        elif option == "4":
            test_autofocus(ps3)
        elif option == "5":
            test_bad_method(ps3)
        elif option == "6":
            break
        else:
            print("Unrecognized option")

    print("Closing")
    ps3.close()


if __name__ == "__main__":
    main()
