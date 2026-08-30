/*
 * 创建时间：2026-08-28
 * 作用：在独立 FreeRTOS 任务中请求服务端匹配和 K 线接口，避免阻塞 LVGL 动画。
 * 修改时间：2026-08-29
 * 修改作用：配网入口迁移至整机 Settings，本服务只负责网络状态和检索请求。
 * 使用方式：submitMatch()/submitDetail() 发起请求，onRunning() 调用 pollTimeout() 并轮询 take*()。
 *
 * 修改时间：2026-08-29
 * 修改作用：服务端缺失所选周期时进入 WaitingData，并自动轮询到后台建库完成；用户仍可随时取消。
 * 使用方式：UI 读取 state()==WaitingData 显示等待提示；A/左滑调用 cancelCurrent() 放弃等待。
 *
 * 修改时间：2026-08-30
 * 修改作用：等待间隔由服务器控制，失败冷却直接转为 Error，不再固定 2.5 秒重复提交匹配。
 */
#pragma once

#include "net_manager.h"
#include "stock_types.h"

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <atomic>
#include <mutex>
#include <string>
#include <vector>

namespace stock_selector {

class ShapeMatchService {
public:
    enum class State {
        Idle,
        Matching,
        WaitingData,
        MatchReady,
        DetailLoading,
        DetailReady,
        Error,
    };

    ShapeMatchService();
    ~ShapeMatchService();

    void initializeNetwork();
    bool isNetworkConnected() const;
    int networkRssi() const;
    std::string networkStatus() const;
    std::string serverUrl() const;

    bool submitMatch(
        const std::vector<NormalizedPoint>& points,
        const std::string& category,
        const std::string& timeframe,
        int limit = 10
    );
    bool submitDetail(const MatchResult& result, const std::string& timeframe, int limit = 200);
    bool cancelCurrent(const std::string& reason = "Request cancelled");
    bool pollTimeout();
    bool isBusy() const;
    uint32_t elapsedMs() const;

    State state() const;
    bool takeMatchResults(std::vector<MatchResult>& results, int& queryMs);
    bool takeDetail(KlineDetail& detail);
    bool takeError(std::string& message);

private:
    enum class RequestType { Match, Detail, Stop };
    struct Request {
        RequestType type = RequestType::Match;
        std::vector<NormalizedPoint> points;
        MatchResult selected;
        std::string category = "all";
        std::string timeframe = "1d";
        int limit = 10;
        uint32_t generation = 0;
    };

    static void workerEntry(void* context);
    void workerLoop();
    void executeMatch(const Request& request);
    void executeDetail(const Request& request);
    void setError(const std::string& message, uint32_t generation = 0);

    NetManager _net;
    QueueHandle_t _queue = nullptr;
    TaskHandle_t _worker = nullptr;
    std::atomic<State> _state{State::Idle};
    std::atomic<uint32_t> _generation{0};
    std::atomic<int64_t> _started_us{0};
    mutable std::mutex _result_mutex;
    std::vector<MatchResult> _match_results;
    KlineDetail _detail;
    std::string _error;
    int _query_ms = 0;
};

}  // namespace stock_selector
