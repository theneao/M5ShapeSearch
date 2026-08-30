#!/usr/bin/env bash
# 形态匹配服务 - 快速启动脚本
#
# 支持参数：
#   start          启动服务
#   stop           停止服务
#   test           运行测试
#   demo           运行演示
#   report         生成验收报告
#   docker         Docker 启动
#   help           显示帮助

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 检查环境
check_env() {
    if ! command -v python &> /dev/null; then
        echo "❌ 未找到 Python，请先安装"
        exit 1
    fi
    echo "✅ Python 版本: $(python --version)"
}

# 启动服务（开发模式）
start_dev() {
    echo "🚀 启动开发服务器 (http://127.0.0.1:8000)..."
    python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
}

# 启动服务（生产模式）
start_prod() {
    echo "🚀 启动生产服务器 (gunicorn)..."
    gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
}

# 运行 API 集成测试
test_api() {
    echo "🧪 运行 API 集成测试..."
    MATCH_SERVER=http://127.0.0.1:8000 python tests/test_shape_match_api.py
}

# 运行本地算法测试
test_pipeline() {
    echo "🧪 运行本地算法链路测试..."
    python tests/test_shape_pipeline.py
}

# 运行演示
demo() {
    echo "🎬 运行完整演示..."
    python demo_end_to_end.py --server-url http://127.0.0.1:8000
}

# 生成报告
report() {
    echo "📊 生成验收报告..."
    python generate_acceptance_report.py --server-url http://127.0.0.1:8000 --output acceptance_report
    echo ""
    echo "📄 报告文件："
    ls -lh acceptance_report.*
}

# Docker 构建
docker_build() {
    echo "🐳 构建 Docker 镜像..."
    docker build -t shape-match-server:latest .
    echo "✅ 镜像构建完成"
}

# Docker 启动
docker_start() {
    echo "🐳 启动 Docker 容器..."
    docker-compose up -d
    echo "✅ 容器已启动"
    docker-compose ps
}

# Docker 停止
docker_stop() {
    echo "🐳 停止 Docker 容器..."
    docker-compose down
    echo "✅ 容器已停止"
}

# 显示帮助
show_help() {
    cat << 'EOF'
形态匹配服务 - 快速启动脚本

使用方式：
    bash run.sh <command> [options]

命令：
    dev          启动开发服务器（含自动重载）
    prod         启动生产服务器（gunicorn）
    test-api     运行 API 集成测试（需先启服务）
    test-pipeline 运行本地算法链路测试（无需服务）
    demo         运行完整端到端演示
    report       生成验收测试报告
    docker-build 构建 Docker 镜像
    docker-start 启动 Docker 容器栈
    docker-stop  停止 Docker 容器栈
    help         显示本帮助

示例：
    # 开发环境
    bash run.sh dev

    # 生产部署
    bash run.sh prod

    # Docker 部署
    bash run.sh docker-build
    bash run.sh docker-start

    # 测试流程
    bash run.sh dev &                    # 后台启动服务
    sleep 3
    bash run.sh demo                     # 运行演示
    bash run.sh report                   # 生成报告

文档：
    README.md           完整文档
    QUICKSTART.md       快速开始指南
    .env.example        环境配置示例
EOF
}

# 主程序
check_env

case "${1:-help}" in
    dev)
        start_dev
        ;;
    prod)
        start_prod
        ;;
    test-api)
        test_api
        ;;
    test-pipeline)
        test_pipeline
        ;;
    demo)
        demo
        ;;
    report)
        report
        ;;
    docker-build)
        docker_build
        ;;
    docker-start)
        docker_start
        ;;
    docker-stop)
        docker_stop
        ;;
    *)
        show_help
        ;;
esac
