@echo off
REM 形态匹配服务 - Windows 快速启动脚本
REM
REM 用法：
REM   run.bat dev              开发模式启动
REM   run.bat prod             生产模式启动
REM   run.bat test             运行测试
REM   run.bat demo             运行演示
REM   run.bat help             显示帮助

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYTHON=python"
set "PORT=8000"

if "%1"=="" (
    goto :show_help
)

if "%1"=="dev" (
    goto :start_dev
)

if "%1"=="prod" (
    goto :start_prod
)

if "%1"=="test-api" (
    goto :test_api
)

if "%1"=="test-pipeline" (
    goto :test_pipeline
)

if "%1"=="demo" (
    goto :demo
)

if "%1"=="report" (
    goto :report
)

if "%1"=="docker-build" (
    goto :docker_build
)

if "%1"=="docker-start" (
    goto :docker_start
)

if "%1"=="docker-stop" (
    goto :docker_stop
)

if "%1"=="help" (
    goto :show_help
)

echo 未知命令: %1
echo 使用 run.bat help 查看帮助
exit /b 1

:start_dev
echo.
echo ^[INFO^] 启动开发服务器 (http://127.0.0.1:8000)...
echo.
%PYTHON% -m uvicorn main:app --host 127.0.0.1 --port %PORT% --reload
goto :end

:start_prod
echo.
echo ^[INFO^] 启动生产服务器 (gunicorn)...
echo.
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:%PORT%
goto :end

:test_api
echo.
echo ^[INFO^] 运行 API 集成测试...
echo.
set "MATCH_SERVER=http://127.0.0.1:8000"
set "PYTHONIOENCODING=utf-8"
%PYTHON% tests/test_shape_match_api.py
goto :end

:test_pipeline
echo.
echo ^[INFO^] 运行本地算法链路测试...
echo.
%PYTHON% tests/test_shape_pipeline.py
goto :end

:demo
echo.
echo ^[INFO^] 运行完整演示...
echo.
set "PYTHONIOENCODING=utf-8"
%PYTHON% demo_end_to_end.py --server-url http://127.0.0.1:8000
goto :end

:report
echo.
echo ^[INFO^] 生成验收报告...
echo.
%PYTHON% generate_acceptance_report.py --server-url http://127.0.0.1:8000 --output acceptance_report
if exist acceptance_report.* (
    echo.
    echo ^[INFO^] 报告文件已生成：
    dir /b acceptance_report.*
)
goto :end

:docker_build
echo.
echo ^[INFO^] 构建 Docker 镜像...
echo.
docker build -t shape-match-server:latest .
if %errorlevel% equ 0 (
    echo ^[OK^] 镜像构建完成
) else (
    echo ^[ERROR^] 镜像构建失败
    exit /b 1
)
goto :end

:docker_start
echo.
echo ^[INFO^] 启动 Docker 容器...
echo.
docker-compose up -d
if %errorlevel% equ 0 (
    echo ^[OK^] 容器已启动
    docker-compose ps
) else (
    echo ^[ERROR^] 容器启动失败
    exit /b 1
)
goto :end

:docker_stop
echo.
echo ^[INFO^] 停止 Docker 容器...
echo.
docker-compose down
if %errorlevel% equ 0 (
    echo ^[OK^] 容器已停止
) else (
    echo ^[ERROR^] 容器停止失败
    exit /b 1
)
goto :end

:show_help
echo.
echo 形态匹配服务 - Windows 快速启动脚本
echo.
echo 使用方式：
echo   run.bat ^<command^>
echo.
echo 可用命令：
echo   dev            开发模式启动（含自动重载）
echo   prod           生产模式启动（gunicorn）
echo   test-api       运行 API 集成测试（需先启服务）
echo   test-pipeline  运行本地算法链路测试（无需服务）
echo   demo           运行完整端到端演示
echo   report         生成验收测试报告
echo   docker-build   构建 Docker 镜像
echo   docker-start   启动 Docker 容器栈
echo   docker-stop    停止 Docker 容器栈
echo   help           显示本帮助
echo.
echo 示例：
echo   rem 开发环境
echo   run.bat dev
echo.
echo   rem Docker 部署
echo   run.bat docker-build
echo   run.bat docker-start
echo.

:end
