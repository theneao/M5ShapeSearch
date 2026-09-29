/*
 * 创建时间：2026-08-28
 * 作用：把画板点提交给 FastAPI，并解析 Top-K 缩略图与单标的 K 线详情。
 * 修改时间：2026-08-29
 * 修改作用：保留 30s 匹配/20s 详情总超时与取消，配网入口统一交由整机 Settings。
 * 使用方式：本文件只负责请求和 JSON 解析；App 每帧调用 pollTimeout()。
 *
 * 修改时间：2026-08-29
 * 修改作用：识别服务端 DATA_BUILDING(202)，按 2.5 秒轮询；建库最多等待 15 分钟且支持即时取消。
 *
 * 修改时间：2026-08-30
 * 修改作用：轮询间隔改为遵守服务器 retry_after_seconds；DATA_UNAVAILABLE 立即结束等待并显示具体原因。
 *
 * 修改时间：2026-08-31
 * 修改作用：匹配总超时延长为 75 秒并继续按信号质量放宽，覆盖服务端完整 NCC/ShapeDTW 计算；
 *           数据建库等待仍单独计时且可取消，避免把正常计算误判为网络失败。
 */
#include "shape_match_service.h"

#include <ArduinoJson.h>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/task.h>
#include <algorithm>
#include <memory>
#include <new>

namespace stock_selector {
namespace {

constexpr const char* kTag = "ShapeService";
constexpr int64_t kMatchTimeoutUs = 75LL * 1000 * 1000;
constexpr int64_t kDetailTimeoutUs = 20LL * 1000 * 1000;
constexpr int64_t kDataBuildTimeoutUs = 15LL * 60 * 1000 * 1000;
constexpr int kHardwareResultLimit = 10;

void logWorkerMemory(const char* stage)
{
    ESP_LOGI(
        kTag,
        "[MEM] %s internal_free=%u largest=%u psram_free=%u stack_low=%u",
        stage,
        static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
        static_cast<unsigned>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
        static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)),
        static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr))
    );
}

float jsonFloat(JsonVariantConst value, float fallback = 0.0f)
{
    return value.is<float>() || value.is<double>() || value.is<int>() ? value.as<float>() : fallback;
}

}  // namespace

ShapeMatchService::ShapeMatchService()
{
    _queue = xQueueCreate(3, sizeof(Request*));
    if (_queue != nullptr) {
        const BaseType_t created = xTaskCreatePinnedToCore(
            &ShapeMatchService::workerEntry,
            "shape_http_worker",
            16 * 1024,
            this,
            3,
            &_worker,
            0
        );
        if (created != pdPASS) {
            ESP_LOGE(kTag, "Cannot create HTTP worker with 16 KB stack");
            vQueueDelete(_queue);
            _queue = nullptr;
            _worker = nullptr;
        }
    }
}

ShapeMatchService::~ShapeMatchService()
{
    if (_queue != nullptr && _worker != nullptr) {
        auto* stop = new Request();
        stop->type = RequestType::Stop;
        xQueueSend(_queue, &stop, 0);
    }
}

void ShapeMatchService::initializeNetwork()
{
    _net.initialize();
}

bool ShapeMatchService::isNetworkConnected() const
{
    return _net.isConnected();
}

int ShapeMatchService::networkRssi() const
{
    return _net.rssi();
}

std::string ShapeMatchService::networkStatus() const
{
    return _net.networkStatus();
}

std::string ShapeMatchService::serverUrl() const
{
    return _net.baseUrl();
}

bool ShapeMatchService::submitMatch(
    const std::vector<NormalizedPoint>& points,
    const std::string& category,
    const std::string& timeframe,
    int limit
)
{
    if (_queue == nullptr || points.size() < 3 || isBusy()) {
        return false;
    }
    auto* request = new (std::nothrow) Request();
    if (request == nullptr) {
        return false;
    }
    request->type = RequestType::Match;
    request->points = points;
    request->category = category;
    request->timeframe = timeframe;
    request->limit = std::clamp(limit, 1, 20);
    request->generation = _generation.fetch_add(1) + 1;
    if (xQueueSend(_queue, &request, 0) != pdTRUE) {
        delete request;
        return false;
    }
    _state.store(State::Matching);
    _started_us.store(esp_timer_get_time());
    return true;
}

bool ShapeMatchService::submitDetail(const MatchResult& result, const std::string& timeframe, int limit)
{
    if (_queue == nullptr || result.symbol.empty() || isBusy()) {
        return false;
    }
    auto* request = new (std::nothrow) Request();
    if (request == nullptr) {
        return false;
    }
    request->type = RequestType::Detail;
    request->selected = result;
    request->timeframe = timeframe;
    request->limit = std::clamp(limit, 30, 300);
    request->generation = _generation.fetch_add(1) + 1;
    if (xQueueSend(_queue, &request, 0) != pdTRUE) {
        delete request;
        return false;
    }
    _state.store(State::DetailLoading);
    _started_us.store(esp_timer_get_time());
    return true;
}

bool ShapeMatchService::submitStrategyCatalog()
{
    if (_queue == nullptr || isBusy()) {
        return false;
    }
    auto* request = new (std::nothrow) Request();
    if (request == nullptr) {
        return false;
    }
    request->type = RequestType::StrategyCatalog;
    request->generation = _generation.fetch_add(1) + 1;
    if (xQueueSend(_queue, &request, 0) != pdTRUE) {
        delete request;
        return false;
    }
    _state.store(State::StrategyCatalogLoading);
    _started_us.store(esp_timer_get_time());
    return true;
}

bool ShapeMatchService::submitStrategyScreen(
    const std::vector<std::string>& strategyIds,
    bool intersection,
    const std::string& category,
    const std::string& timeframe,
    int limit
)
{
    if (_queue == nullptr || strategyIds.empty() || isBusy()) {
        return false;
    }
    auto* request = new (std::nothrow) Request();
    if (request == nullptr) {
        return false;
    }
    request->type = RequestType::StrategyScreen;
    request->strategyIds = strategyIds;
    request->intersection = intersection;
    request->category = category;
    request->timeframe = timeframe;
    request->limit = std::clamp(limit, 1, kHardwareResultLimit);
    request->generation = _generation.fetch_add(1) + 1;
    if (xQueueSend(_queue, &request, 0) != pdTRUE) {
        delete request;
        return false;
    }
    _state.store(State::StrategyScreening);
    _started_us.store(esp_timer_get_time());
    return true;
}

bool ShapeMatchService::submitSaveStrategy(
    const std::vector<NormalizedPoint>& points,
    float threshold
)
{
    if (_queue == nullptr || points.size() < 3 || isBusy()) {
        return false;
    }
    auto* request = new (std::nothrow) Request();
    if (request == nullptr) {
        return false;
    }
    request->type = RequestType::SaveStrategy;
    request->points = points;
    request->limit = static_cast<int>(std::clamp(threshold, 0.0f, 1.0f) * 1000.0f);
    request->generation = _generation.fetch_add(1) + 1;
    if (xQueueSend(_queue, &request, 0) != pdTRUE) {
        delete request;
        return false;
    }
    _state.store(State::StrategySaving);
    _started_us.store(esp_timer_get_time());
    return true;
}

bool ShapeMatchService::isBusy() const
{
    const State value = _state.load();
    return value == State::Matching || value == State::WaitingData ||
        value == State::StrategyCatalogLoading || value == State::StrategyScreening ||
        value == State::StrategySaving || value == State::DetailLoading;
}

uint32_t ShapeMatchService::elapsedMs() const
{
    const int64_t started = _started_us.load();
    if (!isBusy() || started <= 0) {
        return 0;
    }
    return static_cast<uint32_t>((esp_timer_get_time() - started) / 1000);
}

bool ShapeMatchService::cancelCurrent(const std::string& reason)
{
    if (!isBusy()) {
        return false;
    }
    _generation.fetch_add(1);
    _net.cancelCurrentRequest();
    {
        std::lock_guard<std::mutex> lock(_result_mutex);
        _error = reason;
    }
    _started_us.store(0);
    _state.store(State::Error);
    return true;
}

bool ShapeMatchService::pollTimeout()
{
    const State value = _state.load();
    if (value != State::Matching && value != State::WaitingData &&
        value != State::StrategyCatalogLoading && value != State::StrategyScreening &&
        value != State::StrategySaving && value != State::DetailLoading) {
        return false;
    }
    int64_t limit = value == State::WaitingData ? kDataBuildTimeoutUs :
        (value == State::Matching || value == State::StrategyScreening) ? kMatchTimeoutUs :
        kDetailTimeoutUs;
    const int signal = _net.rssi();
    if (signal <= -80) {
        limit *= 2;
    } else if (signal <= -70) {
        limit = limit * 3 / 2;
    }
    const int64_t started = _started_us.load();
    if (started > 0 && esp_timer_get_time() - started >= limit) {
        return cancelCurrent(
            value == State::WaitingData
                ? "Data build timed out; retry later"
                : (value == State::Matching || value == State::StrategyScreening)
                ? "Search timed out; check WiFi/server"
                : "Kline timed out; check WiFi/server"
        );
    }
    return false;
}

ShapeMatchService::State ShapeMatchService::state() const
{
    return _state.load();
}

bool ShapeMatchService::takeMatchResults(std::vector<MatchResult>& results, int& queryMs)
{
    if (_state.load() != State::MatchReady) {
        return false;
    }
    std::lock_guard<std::mutex> lock(_result_mutex);
    results = _match_results;
    queryMs = _query_ms;
    _state.store(State::Idle);
    return true;
}

bool ShapeMatchService::takeStrategyCatalog(std::vector<StrategyDefinition>& strategies)
{
    if (_state.load() != State::StrategyCatalogReady) {
        return false;
    }
    std::lock_guard<std::mutex> lock(_result_mutex);
    strategies = _strategies;
    _state.store(State::Idle);
    return true;
}

bool ShapeMatchService::takeStrategyResults(std::vector<MatchResult>& results, int& queryMs)
{
    if (_state.load() != State::StrategyReady) {
        return false;
    }
    std::lock_guard<std::mutex> lock(_result_mutex);
    results = _match_results;
    queryMs = _query_ms;
    _state.store(State::Idle);
    return true;
}

bool ShapeMatchService::takeStrategySaved()
{
    if (_state.load() != State::StrategySaved) {
        return false;
    }
    _state.store(State::Idle);
    return true;
}

bool ShapeMatchService::takeDetail(KlineDetail& detail)
{
    if (_state.load() != State::DetailReady) {
        return false;
    }
    std::lock_guard<std::mutex> lock(_result_mutex);
    detail = _detail;
    _state.store(State::Idle);
    return true;
}

bool ShapeMatchService::takeError(std::string& message)
{
    if (_state.load() != State::Error) {
        return false;
    }
    std::lock_guard<std::mutex> lock(_result_mutex);
    message = _error;
    _state.store(State::Idle);
    return true;
}

void ShapeMatchService::workerEntry(void* context)
{
    static_cast<ShapeMatchService*>(context)->workerLoop();
}

void ShapeMatchService::workerLoop()
{
    while (true) {
        Request* raw_request = nullptr;
        if (xQueueReceive(_queue, &raw_request, portMAX_DELAY) != pdTRUE || raw_request == nullptr) {
            continue;
        }
        std::unique_ptr<Request> request(raw_request);
        if (request->type == RequestType::Stop) {
            break;
        }
        if (request->type == RequestType::Match) {
            executeMatch(*request);
        } else if (request->type == RequestType::Detail) {
            executeDetail(*request);
        } else if (request->type == RequestType::StrategyCatalog) {
            executeStrategyCatalog(*request);
        } else if (request->type == RequestType::StrategyScreen) {
            executeStrategyScreen(*request);
        } else if (request->type == RequestType::SaveStrategy) {
            executeSaveStrategy(*request);
        }
    }
    _worker = nullptr;
    vTaskDelete(nullptr);
}

void ShapeMatchService::executeMatch(const Request& request)
{
    JsonDocument document;
    JsonArray points = document["points"].to<JsonArray>();
    for (const auto& point : request.points) {
        JsonArray pair = points.add<JsonArray>();
        pair.add(point.x);
        pair.add(point.y);
    }
    document["category"] = request.category;
    document["timeframe"] = request.timeframe;
    document["limit"] = request.limit;
    document["compact"] = true;

    std::string body;
    serializeJson(document, body);
    JsonDocument payload;
    while (true) {
        if (request.generation != _generation.load()) {
            return;
        }
        std::string response;
        std::string error;
        if (!_net.postJson("/api/v1/shape/match", body, response, error)) {
            setError(error, request.generation);
            return;
        }
        payload.clear();
        const DeserializationError parse_error = deserializeJson(payload, response);
        if (parse_error) {
            setError(std::string("Invalid match JSON: ") + parse_error.c_str(), request.generation);
            return;
        }
        if (payload["code"].as<int>() != 1001) {
            break;
        }
        _state.store(State::WaitingData);
        const int retry_seconds = std::clamp(
            payload["data"]["retry_after_seconds"] | 15,
            5,
            300
        );
        ESP_LOGI(kTag, "Server is building %s data; retry in %ds",
                 request.timeframe.c_str(), retry_seconds);
        for (int tick = 0; tick < retry_seconds * 10; ++tick) {
            if (request.generation != _generation.load()) {
                return;
            }
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    if (payload["code"].as<int>() != 0) {
        std::string server_message = payload["data"]["message"] | "";
        if (server_message.empty()) {
            server_message = payload["msg"] | "Server rejected match request";
        }
        setError(server_message, request.generation);
        return;
    }

    std::vector<MatchResult> parsed;
    const JsonArrayConst items = payload["data"]["list"].as<JsonArrayConst>();
    for (JsonObjectConst item : items) {
        MatchResult result;
        result.symbol = item["symbol"] | "";
        result.name = item["name"] | "";
        result.category = item["category"] | "";
        result.matchScore = jsonFloat(item["match_score"]);
        result.currentPrice = jsonFloat(item["current_price"]);
        result.changePct = jsonFloat(item["change_pct"]);
        result.windowStartTs = item["window_start_ts"] | 0LL;
        result.windowEndTs = item["window_end_ts"] | 0LL;
        result.rawBars = item["raw_bars"] | 0;
        JsonObjectConst scores = item["score_details"];
        result.details.priceNcc = jsonFloat(scores["price_ncc"]);
        result.details.derivativeNcc = jsonFloat(scores["derivative_ncc"]);
        result.details.turningScore = jsonFloat(scores["turning_score"]);
        result.details.shapeDtwScore = jsonFloat(scores["shapedtw_score"]);
        result.details.shapeDtwDist = jsonFloat(scores["shapedtw_distance"]);
        const JsonArrayConst preview_points = item["preview_points"].as<JsonArrayConst>();
        for (JsonArrayConst pair : preview_points) {
            if (pair.size() >= 2) {
                result.preview.push_back({jsonFloat(pair[0]), jsonFloat(pair[1])});
            }
        }
        parsed.push_back(std::move(result));
    }

    if (request.generation != _generation.load()) {
        ESP_LOGW(kTag, "Discard cancelled match response, generation=%u", request.generation);
        return;
    }
    {
        std::lock_guard<std::mutex> lock(_result_mutex);
        _match_results = std::move(parsed);
        _query_ms = payload["data"]["query_ms"] | 0;
    }
    ESP_LOGI(kTag, "Parsed %u match results", static_cast<unsigned>(_match_results.size()));
    _started_us.store(0);
    _state.store(State::MatchReady);
}

void ShapeMatchService::executeDetail(const Request& request)
{
    std::string path = "/api/v1/market/kline?symbol=" + NetManager::urlEncode(request.selected.symbol)
        + "&tf=" + NetManager::urlEncode(request.timeframe)
        + "&limit=" + std::to_string(request.limit);
    if (request.selected.windowStartTs > 0) {
        path += "&from_ts=" + std::to_string(request.selected.windowStartTs);
    }
    if (request.selected.windowEndTs > 0) {
        path += "&to_ts=" + std::to_string(request.selected.windowEndTs);
    }
    std::string response;
    std::string error;
    if (!_net.get(path, response, error)) {
        setError(error, request.generation);
        return;
    }

    JsonDocument payload;
    const DeserializationError parse_error = deserializeJson(payload, response);
    if (parse_error) {
        setError(std::string("Invalid kline JSON: ") + parse_error.c_str(), request.generation);
        return;
    }
    if (payload["code"].as<int>() != 0) {
        setError(payload["msg"] | "Server rejected detail request", request.generation);
        return;
    }

    JsonObjectConst data = payload["data"];
    KlineDetail detail;
    detail.symbol = data["symbol"] | request.selected.symbol;
    detail.name = data["name"] | request.selected.name;
    detail.category = data["category"] | request.selected.category;
    detail.timeframe = data["tf"] | request.timeframe;
    detail.hasMore = data["has_more"] | false;
    detail.ohlcExact = data["ohlc_exact"] | false;
    const JsonArrayConst bars = data["bars"].as<JsonArrayConst>();
    for (JsonObjectConst item : bars) {
        KlineBar bar;
        bar.timestamp = item["t"] | 0LL;
        bar.open = jsonFloat(item["o"]);
        bar.high = jsonFloat(item["h"]);
        bar.low = jsonFloat(item["l"]);
        bar.close = jsonFloat(item["c"]);
        bar.volume = jsonFloat(item["v"]);
        detail.bars.push_back(bar);
    }
    if (request.generation != _generation.load()) {
        ESP_LOGW(kTag, "Discard cancelled detail response, generation=%u", request.generation);
        return;
    }
    {
        std::lock_guard<std::mutex> lock(_result_mutex);
        _detail = std::move(detail);
    }
    _started_us.store(0);
    _state.store(State::DetailReady);
}

void ShapeMatchService::executeStrategyCatalog(const Request& request)
{
    std::string response;
    std::string error;
    if (!_net.get("/api/v1/strategies/catalog?compact=true", response, error)) {
        setError(error, request.generation);
        return;
    }
    JsonDocument payload;
    const DeserializationError parse_error = deserializeJson(payload, response);
    if (parse_error || payload["code"].as<int>() != 0) {
        setError(parse_error ? std::string("Invalid strategy JSON: ") + parse_error.c_str()
                             : "Server rejected strategy catalog", request.generation);
        return;
    }
    std::vector<StrategyDefinition> parsed;
    auto append_items = [&parsed](JsonArrayConst items) {
        for (JsonObjectConst item : items) {
            StrategyDefinition strategy;
            strategy.id = item["id"] | "";
            strategy.name = item["name"] | "";
            strategy.shortName = item["short_name"] | "STRATEGY";
            strategy.kind = item["kind"] | "preset";
            if (!strategy.id.empty()) {
                parsed.push_back(std::move(strategy));
            }
        }
    };
    append_items(payload["data"]["presets"].as<JsonArrayConst>());
    append_items(payload["data"]["saved_sketches"].as<JsonArrayConst>());
    if (request.generation != _generation.load()) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(_result_mutex);
        _strategies = std::move(parsed);
    }
    _started_us.store(0);
    _state.store(State::StrategyCatalogReady);
}

void ShapeMatchService::executeStrategyScreen(const Request& request)
{
    logWorkerMemory("strategy-start");
    JsonDocument document;
    JsonArray ids = document["strategy_ids"].to<JsonArray>();
    for (const auto& id : request.strategyIds) {
        ids.add(id);
    }
    document["combine"] = request.intersection ? "intersection" : "union";
    document["category"] = request.category;
    document["timeframe"] = request.timeframe;
    document["limit"] = request.limit;
    std::string body;
    serializeJson(document, body);
    JsonDocument payload;
    while (true) {
        if (request.generation != _generation.load()) {
            return;
        }
        std::string response;
        std::string error;
        if (!_net.postJson("/api/v1/strategies/screen", body, response, error)) {
            setError(error, request.generation);
            return;
        }
        payload.clear();
        const DeserializationError parse_error = deserializeJson(payload, response);
        if (parse_error) {
            setError(std::string("Invalid screen JSON: ") + parse_error.c_str(), request.generation);
            return;
        }
        if (payload["code"].as<int>() != 1001) {
            break;
        }
        _state.store(State::WaitingData);
        const int retry_seconds = std::clamp(
            payload["data"]["retry_after_seconds"] | 15,
            5,
            300
        );
        for (int tick = 0; tick < retry_seconds * 10; ++tick) {
            if (request.generation != _generation.load()) {
                return;
            }
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    if (payload["code"].as<int>() != 0) {
        setError(payload["msg"] | "Strategy screen failed", request.generation);
        return;
    }
    std::vector<MatchResult> parsed;
    const JsonArrayConst items = payload["data"]["list"].as<JsonArrayConst>();
    parsed.reserve(std::min<std::size_t>(items.size(), static_cast<std::size_t>(request.limit)));
    for (JsonObjectConst item : items) {
        if (parsed.size() >= static_cast<std::size_t>(request.limit)) {
            break;
        }
        MatchResult result;
        result.symbol = item["symbol"] | "";
        result.name = item["name"] | "";
        result.category = item["category"] | "";
        result.currentPrice = jsonFloat(item["current_price"]);
        result.changePct = jsonFloat(item["change_pct"]);
        result.matchScore = jsonFloat(item["combined_score"]);
        result.rawBars = item["coverage"] | 0;
        parsed.push_back(std::move(result));
    }
    if (request.generation != _generation.load()) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(_result_mutex);
        _match_results = std::move(parsed);
        _query_ms = payload["data"]["query_ms"] | 0;
    }
    logWorkerMemory("strategy-ready");
    _started_us.store(0);
    _state.store(State::StrategyReady);
}

void ShapeMatchService::executeSaveStrategy(const Request& request)
{
    JsonDocument document;
    document["name"] = "Watch sketch";
    document["threshold"] = static_cast<float>(request.limit) / 1000.0f;
    document["max_results"] = 60;
    JsonArray points = document["points"].to<JsonArray>();
    for (const auto& point : request.points) {
        JsonArray pair = points.add<JsonArray>();
        pair.add(point.x);
        pair.add(point.y);
    }
    std::string body;
    serializeJson(document, body);
    std::string response;
    std::string error;
    if (!_net.postJson("/api/v1/strategies/sketches", body, response, error)) {
        setError(error, request.generation);
        return;
    }
    JsonDocument payload;
    const DeserializationError parse_error = deserializeJson(payload, response);
    if (parse_error || payload["code"].as<int>() != 0) {
        setError(parse_error ? std::string("Invalid save JSON: ") + parse_error.c_str()
                             : "Server rejected saved shape", request.generation);
        return;
    }
    if (request.generation != _generation.load()) {
        return;
    }
    _started_us.store(0);
    _state.store(State::StrategySaved);
}

void ShapeMatchService::setError(const std::string& message, uint32_t generation)
{
    if (generation != 0 && generation != _generation.load()) {
        return;
    }
    ESP_LOGE(kTag, "%s", message.c_str());
    {
        std::lock_guard<std::mutex> lock(_result_mutex);
        _error = message;
    }
    _started_us.store(0);
    _state.store(State::Error);
}

}  // namespace stock_selector
