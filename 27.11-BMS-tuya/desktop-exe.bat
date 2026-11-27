@REM  pyinstaller --noconsole --onefile --add-data "static;static" --name "PowerMonitor" desktop.py

@REM  pyinstaller --noconsole --onefile --icon="icon.ico" --add-data "static;static" --add-data "icon.ico;." --name "PowerMonitor" desktop.py

@REM pyinstaller --noconsole --onefile --add-data "static;static" --hidden-import "bleak.backends.winrt" --hidden-import "bleak.backends.winrt.scanner" --hidden-import "bleak.backends.winrt.client" --name "PowerMonitor" desktop.py

@REM python -m pip install pywebview

python -m PyInstaller --noconsole --onefile --add-data "static;static" --hidden-import "bleak.backends.winrt" --hidden-import "bleak.backends.winrt.scanner" --hidden-import "bleak.backends.winrt.client" --hidden-import "tinytuya" --name "PowerMonitor" desktop.py