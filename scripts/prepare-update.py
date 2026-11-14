"""Gracefully close local capture before updating. Never logs into the camera."""
import time
import httpx


def main():
    with httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=5) as client:
        try:
            response = client.get("/")
        except httpx.ConnectError:
            return
        if response.status_code != 200 or response.json().get("service") != "VesselSight Operations API":
            raise RuntimeError("Port 8010 is occupied by another service")
        status = client.get("/api/realtime/camera").raise_for_status().json()
        if status["status"] in {"running", "starting", "reconnecting"}:
            client.post("/api/realtime/camera/stop").raise_for_status()
        local = client.get("/api/realtime/local-video").raise_for_status().json()
        if local["status"] in {"running", "starting"}:
            client.post("/api/realtime/local-video/stop").raise_for_status()
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            recording = client.get("/api/recording/status").raise_for_status().json()
            if recording["phase"] in {"overview", "error"} and not recording.get("media_recording"):
                print("Local capture drained; ready to update.")
                return
            time.sleep(.25)
        raise RuntimeError("Capture or lens recovery is still busy; update was not started")


if __name__ == "__main__":
    main()
