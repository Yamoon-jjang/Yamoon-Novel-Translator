@echo off
setlocal
cd /d "%~dp0"

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [1/13] Static prebuild gate...
python tests\prebuild_gate.py
if errorlevel 1 exit /b 1

echo [2/13] Unit and regression tests...
python -m pytest -q
if errorlevel 1 exit /b 1

echo [3/13] Qt-free production worker end-to-end audit...
python tests\job_flow_audit.py
if errorlevel 1 exit /b 1

echo [4/13] 20,000-request failure-injection stress audit...
python tests\stress_audit.py
if errorlevel 1 exit /b 1

echo [5/13] 750-case adversarial TXT fuzz audit...
python tests\fuzz_text_audit.py
if errorlevel 1 exit /b 1

echo [6/13] Release scenario matrix audit...
python tests\release_matrix_audit.py
if errorlevel 1 exit /b 1

echo [7/13] Python bytecode compile check...
python -m compileall -q novel_translator run.py tests
if errorlevel 1 exit /b 1

echo [8/13] PyInstaller ONEDIR build...
python tools\stamp_build.py
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean NovelTranslator.spec
if errorlevel 1 exit /b 1

if not exist "dist\NovelTranslator\config" mkdir "dist\NovelTranslator\config"
if not exist "dist\NovelTranslator\profiles" mkdir "dist\NovelTranslator\profiles"
xcopy /e /i /y "config\*" "dist\NovelTranslator\config\" >nul
xcopy /e /i /y "profiles\*" "dist\NovelTranslator\profiles\" >nul

echo [9/13] Built EXE runtime self-test...
"dist\NovelTranslator\NovelTranslator.exe" --self-test
if errorlevel 1 exit /b 1

echo [10/13] Built EXE translation-engine self-test...
"dist\NovelTranslator\NovelTranslator.exe" --engine-self-test
if errorlevel 1 exit /b 1

echo [11/13] Built EXE Windows secret-store/DPAPI self-test...
"dist\NovelTranslator\NovelTranslator.exe" --secret-self-test
if errorlevel 1 exit /b 1

echo [12/13] Built EXE Qt/MainWindow self-test...
"dist\NovelTranslator\NovelTranslator.exe" --gui-self-test
if errorlevel 1 exit /b 1

echo [13/13] Relocated portable folder test (spaces + Korean path)...
powershell -NoProfile -ExecutionPolicy Bypass -File tests\postbuild_windows.ps1
if errorlevel 1 exit /b 1

echo.
echo Build, tests, runtime/engine/secret-store/GUI/relocation self-tests completed.
echo User package requires no Python or pip.
echo Output: dist\NovelTranslator\
