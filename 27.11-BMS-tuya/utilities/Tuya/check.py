# pip install tinytuya
import tinytuya

c = tinytuya.Cloud(
    apiRegion="eu",
    apiKey="caxvvqpmmqe3pac7awj3",
    apiSecret="18fc2da9326c42289acba34b7693d5da",
    apiDeviceID="bf58c819facd06078dwi5x",
)

res = c.getstatus("bf58c819facd06078dwi5x")
data = {item["code"]: item["value"] for item in res.get("result", [])}

# Перетворення у зручний формат
temp_in = data.get("temp_current", 0) / 10.0
temp_ext = data.get("temp_current_external", 0) / 10.0
humidity = data.get("humidity_value", 0)

battery_map = {
    "high": "Високий (100%)",
    "middle": "Середній (50%)",
    "low": "Низький (замініть батарейки)",
}
battery = battery_map.get(data.get("battery_state"), data.get("battery_state"))

print(f"🏠 Кімната:  {temp_in:+.1f}°C")
print(f"💧 Вологість: {humidity}%")
print(f"🌡️ Зонд:     {temp_ext:+.1f}°C")
print(f"🔋 Батарея:  {battery}")
