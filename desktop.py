import threading
import time
import uvicorn
import webview
from app import app

def start_server():
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="error")

if __name__ == "__main__":
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    time.sleep(1)  # Даємо серверу секунду на старт

    webview.create_window(
        title="Моніторинг світла — Полтава (Черга 1.1)",
        url="http://127.0.0.1:8765",
        width=1100,
        height=740,
        min_size=(800, 620),
        background_color="#0b0f17",
    )
    webview.start()