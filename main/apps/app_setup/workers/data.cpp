/*
 * 创建时间：2026-08-29
 * 修改时间：2026-08-29
 * 作用：在手表端直接编辑含分钟周期的服务器市场数据参数；本地 NVS 持久化，后台 HTTP GET/PUT 同步并触发刷新。
 * 使用方式：Settings -> Market Data；A/B 或屏幕箭头改值，点 PREV/NEXT FIELD 切字段，APPLY 保存，BACK/左滑返回。
 *
 * 修改时间：2026-08-29
 * 修改作用：补齐服务器数据参数、A 股板块和 Crypto 排序选项；数量 0 显示为 ALL，并增加明确的前/后字段触控导航。
 *
 * 修改时间：2026-08-30
 * 修改作用：A 股中文板块在手表上改为英文序号显示，避免依赖大体积 CJK 字库及缺字方框。
 * 使用方式：配置值仍向服务器保存原始板块名，屏幕仅显示 CN SECTOR 序号。
 *
 * 修改时间：2026-08-30
 * 修改作用：数据设置只更新实际变化的字段标签，避免每次按键重复刷新三个文本对象。
 */
#include "workers.h"

#include <apps/app_stock_selector/net_manager.h>
#include <hal/utils/settings/settings.h>
#include <assets/assets.h>
#include <ArduinoJson.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <mutex>
#include <string>
#include <vector>

using namespace smooth_ui_toolkit::lvgl_cpp;
using namespace setup_workers;

namespace setup_workers {
namespace {

constexpr const char* kDataNamespace = "shape_data";
constexpr std::array<const char*, 7> kTimeframes = {
    "1d", "1w", "4h", "60m", "30m", "15m", "5m"
};
constexpr std::array<int, 4> kWindows = {30, 60, 90, 120};
constexpr std::array<int, 5> kFetchBars = {60, 120, 300, 500, 1000};
constexpr std::array<int, 10> kCounts = {0, 10, 30, 50, 100, 300, 500, 1000, 2000, 3000};
constexpr std::array<const char*, 3> kStockMetrics = {"market_cap", "volume", "volume_ratio"};
constexpr std::array<const char*, 4> kCryptoMetrics = {
    "volume", "base_volume", "trade_count", "change_pct"
};
constexpr std::array<const char*, 3> kQuoteAssets = {"USDT", "USDC", "FDUSD"};
constexpr std::array<int, 6> kRefreshSeconds = {60, 120, 300, 600, 1800, 3600};
constexpr std::array<int, 7> kBinanceIntervals = {50, 100, 150, 250, 500, 1000, 2000};
constexpr std::array<int, 6> kBinanceWeights = {100, 600, 1200, 2400, 3600, 4800};
constexpr std::array<int, 5> kBinanceConcurrency = {1, 2, 4, 6, 8};
constexpr std::array<int, 6> kBinanceRetries = {0, 1, 2, 3, 4, 5};
constexpr std::array<int, 5> kRetryAfterSeconds = {15, 30, 60, 120, 300};

struct MarketDataConfig {
    uint32_t timeframeMask = 1U;
    int featureWindow = 1;
    int fetchBars = 2;
    bool adaptive = true;
    int refreshSeconds = 2;
    bool stockEnabled = true;
    int stockCount = 5;
    int stockMetric = 0;
    bool stockTop = true;
    std::string stockSector = "all";
    bool cryptoEnabled = true;
    int cryptoCount = 5;
    int cryptoMetric = 0;
    bool cryptoTop = true;
    int quoteAsset = 0;
    int binanceInterval = 2;
    int binanceWeight = 2;
    int binanceConcurrency = 2;
    int binanceRetry = 3;
    int retryAfterSeconds = 2;
};

template <typename T, std::size_t N, typename U>
int nearestIndex(const std::array<T, N>& values, U value)
{
    int best = 0;
    auto best_distance = std::abs(static_cast<long long>(values[0]) - value);
    for (std::size_t index = 1; index < values.size(); ++index) {
        const auto distance = std::abs(static_cast<long long>(values[index]) - value);
        if (distance < best_distance) {
            best = static_cast<int>(index);
            best_distance = distance;
        }
    }
    return best;
}

template <std::size_t N>
int stringIndex(const std::array<const char*, N>& values, const std::string& value, int fallback)
{
    for (std::size_t index = 0; index < values.size(); ++index) {
        if (value == values[index]) {
            return static_cast<int>(index);
        }
    }
    return fallback;
}

uint32_t parseTimeframeMask(std::string value)
{
    uint32_t mask = 0;
    std::size_t start = 0;
    while (start <= value.size()) {
        const std::size_t comma = value.find(',', start);
        std::string token = value.substr(
            start, comma == std::string::npos ? std::string::npos : comma - start
        );
        if (token == "1h") token = "60m";
        const int index = stringIndex(kTimeframes, token, -1);
        if (index >= 0) {
            mask |= 1U << index;
        }
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    return mask == 0 ? 1U : mask;
}

std::string serializeTimeframeMask(uint32_t mask)
{
    std::string value;
    for (std::size_t index = 0; index < kTimeframes.size(); ++index) {
        if ((mask & (1U << index)) == 0) continue;
        if (!value.empty()) value += ',';
        value += kTimeframes[index];
    }
    return value.empty() ? std::string("1d") : value;
}

MarketDataConfig loadLocalConfig()
{
    Settings settings(kDataNamespace, false);
    MarketDataConfig config;
    const std::string saved_timeframes = settings.GetString(
        "tfs", settings.GetString("tf", "1d")
    );
    config.timeframeMask = parseTimeframeMask(saved_timeframes);
    config.featureWindow = nearestIndex(kWindows, settings.GetInt("win", 60));
    config.fetchBars = nearestIndex(kFetchBars, settings.GetInt("bars", 300));
    config.adaptive = settings.GetBool("adapt", true);
    config.refreshSeconds = nearestIndex(kRefreshSeconds, settings.GetInt("refresh", 300));
    config.stockEnabled = settings.GetBool("s_en", true);
    config.stockCount = nearestIndex(kCounts, settings.GetInt("s_count", 300));
    config.stockMetric = stringIndex(kStockMetrics, settings.GetString("s_metric", "market_cap"), 0);
    config.stockTop = settings.GetString("s_order", "top") != "bottom";
    config.stockSector = settings.GetString("s_sector", "all");
    config.cryptoEnabled = settings.GetBool("c_en", true);
    config.cryptoCount = nearestIndex(kCounts, settings.GetInt("c_count", 300));
    config.cryptoMetric = stringIndex(kCryptoMetrics, settings.GetString("c_metric", "volume"), 0);
    config.cryptoTop = settings.GetString("c_order", "top") != "bottom";
    config.quoteAsset = stringIndex(kQuoteAssets, settings.GetString("c_quote", "USDT"), 0);
    config.binanceInterval = nearestIndex(kBinanceIntervals, settings.GetInt("b_interval", 150));
    config.binanceWeight = nearestIndex(kBinanceWeights, settings.GetInt("b_weight", 1200));
    config.binanceConcurrency = nearestIndex(kBinanceConcurrency, settings.GetInt("b_concur", 4));
    config.binanceRetry = nearestIndex(kBinanceRetries, settings.GetInt("b_retry", 3));
    config.retryAfterSeconds = nearestIndex(kRetryAfterSeconds, settings.GetInt("b_after", 60));
    return config;
}

void saveLocalConfig(const MarketDataConfig& config, bool pending)
{
    Settings settings(kDataNamespace, true);
    settings.SetString("tfs", serializeTimeframeMask(config.timeframeMask));
    settings.SetInt("win", kWindows[config.featureWindow]);
    settings.SetInt("bars", std::max(kFetchBars[config.fetchBars], kWindows[config.featureWindow]));
    settings.SetBool("adapt", config.adaptive);
    settings.SetInt("refresh", kRefreshSeconds[config.refreshSeconds]);
    settings.SetBool("s_en", config.stockEnabled);
    settings.SetInt("s_count", kCounts[config.stockCount]);
    settings.SetString("s_metric", kStockMetrics[config.stockMetric]);
    settings.SetString("s_order", config.stockTop ? "top" : "bottom");
    settings.SetString("s_sector", config.stockSector.empty() ? "all" : config.stockSector);
    settings.SetBool("c_en", config.cryptoEnabled);
    settings.SetInt("c_count", kCounts[config.cryptoCount]);
    settings.SetString("c_metric", kCryptoMetrics[config.cryptoMetric]);
    settings.SetString("c_order", config.cryptoTop ? "top" : "bottom");
    settings.SetString("c_quote", kQuoteAssets[config.quoteAsset]);
    settings.SetInt("b_interval", kBinanceIntervals[config.binanceInterval]);
    settings.SetInt("b_weight", kBinanceWeights[config.binanceWeight]);
    settings.SetInt("b_concur", kBinanceConcurrency[config.binanceConcurrency]);
    settings.SetInt("b_retry", kBinanceRetries[config.binanceRetry]);
    settings.SetInt("b_after", kRetryAfterSeconds[config.retryAfterSeconds]);
    settings.SetBool("pending", pending);
}

bool hasPendingConfig()
{
    Settings settings(kDataNamespace, false);
    return settings.GetBool("pending", false);
}

std::string serializeConfig(const MarketDataConfig& config)
{
    JsonDocument document;
    JsonArray timeframes = document["timeframes"].to<JsonArray>();
    for (std::size_t index = 0; index < kTimeframes.size(); ++index) {
        if ((config.timeframeMask & (1U << index)) != 0) {
            timeframes.add(kTimeframes[index]);
        }
    }
    if (timeframes.size() == 0) timeframes.add("1d");
    document["feature_window"] = kWindows[config.featureWindow];
    document["fetch_bars"] = std::max(kFetchBars[config.fetchBars], kWindows[config.featureWindow]);
    document["adaptive_scale"] = config.adaptive;
    document["refresh_check_seconds"] = kRefreshSeconds[config.refreshSeconds];
    JsonObject stock = document["stock"].to<JsonObject>();
    stock["enabled"] = config.stockEnabled;
    stock["count"] = kCounts[config.stockCount];
    stock["rank_metric"] = kStockMetrics[config.stockMetric];
    stock["rank_order"] = config.stockTop ? "top" : "bottom";
    stock["sector"] = config.stockSector.empty() ? "all" : config.stockSector;
    JsonObject crypto = document["crypto"].to<JsonObject>();
    crypto["enabled"] = config.cryptoEnabled;
    crypto["count"] = kCounts[config.cryptoCount];
    crypto["rank_metric"] = kCryptoMetrics[config.cryptoMetric];
    crypto["rank_order"] = config.cryptoTop ? "top" : "bottom";
    crypto["quote_asset"] = kQuoteAssets[config.quoteAsset];
    crypto["binance_request_interval_ms"] = kBinanceIntervals[config.binanceInterval];
    crypto["binance_weight_limit_per_minute"] = kBinanceWeights[config.binanceWeight];
    crypto["binance_concurrency"] = kBinanceConcurrency[config.binanceConcurrency];
    crypto["binance_retry_count"] = kBinanceRetries[config.binanceRetry];
    crypto["binance_max_retry_after_seconds"] = kRetryAfterSeconds[config.retryAfterSeconds];
    std::string body;
    serializeJson(document, body);
    return body;
}

bool parseConfig(const std::string& response, MarketDataConfig& config, std::string& error)
{
    JsonDocument document;
    const DeserializationError parse_error = deserializeJson(document, response);
    if (parse_error) {
        error = std::string("Invalid config JSON: ") + parse_error.c_str();
        return false;
    }
    if ((document["code"] | -1) != 0) {
        error = document["msg"] | "Server rejected data config";
        return false;
    }
    JsonObjectConst data = document["data"];
    JsonArrayConst timeframes = data["timeframes"].as<JsonArrayConst>();
    if (!timeframes.isNull() && timeframes.size() > 0) {
        uint32_t mask = 0;
        for (JsonVariantConst item : timeframes) {
            std::string timeframe = item.as<std::string>();
            if (timeframe == "1h") timeframe = "60m";
            const int index = stringIndex(kTimeframes, timeframe, -1);
            if (index >= 0) mask |= 1U << index;
        }
        if (mask != 0) config.timeframeMask = mask;
    }
    config.featureWindow = nearestIndex(kWindows, data["feature_window"] | 60);
    config.fetchBars = nearestIndex(kFetchBars, data["fetch_bars"] | 300);
    config.adaptive = data["adaptive_scale"] | true;
    config.refreshSeconds = nearestIndex(kRefreshSeconds, data["refresh_check_seconds"] | 300);
    JsonObjectConst stock = data["stock"];
    config.stockEnabled = stock["enabled"] | true;
    config.stockCount = nearestIndex(kCounts, stock["count"] | 300);
    config.stockMetric = stringIndex(kStockMetrics, stock["rank_metric"] | "market_cap", 0);
    config.stockTop = std::string(stock["rank_order"] | "top") != "bottom";
    config.stockSector = stock["sector"] | "all";
    JsonObjectConst crypto = data["crypto"];
    config.cryptoEnabled = crypto["enabled"] | true;
    config.cryptoCount = nearestIndex(kCounts, crypto["count"] | 300);
    config.cryptoMetric = stringIndex(kCryptoMetrics, crypto["rank_metric"] | "volume", 0);
    config.cryptoTop = std::string(crypto["rank_order"] | "top") != "bottom";
    config.quoteAsset = stringIndex(kQuoteAssets, crypto["quote_asset"] | "USDT", 0);
    config.binanceInterval = nearestIndex(
        kBinanceIntervals, crypto["binance_request_interval_ms"] | 150
    );
    config.binanceWeight = nearestIndex(
        kBinanceWeights, crypto["binance_weight_limit_per_minute"] | 1200
    );
    config.binanceConcurrency = nearestIndex(
        kBinanceConcurrency, crypto["binance_concurrency"] | 4
    );
    config.binanceRetry = nearestIndex(kBinanceRetries, crypto["binance_retry_count"] | 3);
    config.retryAfterSeconds = nearestIndex(
        kRetryAfterSeconds, crypto["binance_max_retry_after_seconds"] | 60
    );
    return true;
}

bool parseSectors(const std::string& response, std::vector<std::string>& sectors)
{
    JsonDocument document;
    if (deserializeJson(document, response) || (document["code"] | -1) != 0) {
        return false;
    }
    JsonArrayConst items = document["data"]["items"].as<JsonArrayConst>();
    if (items.isNull()) {
        return false;
    }
    sectors.clear();
    sectors.emplace_back("all");
    for (JsonVariantConst item : items) {
        const std::string value = item.as<std::string>();
        if (!value.empty() && value != "all") {
            sectors.push_back(value);
        }
    }
    return true;
}

}  // namespace

class DataSettingsWorker::DataContext {
public:
    enum class Result { None, Loaded, Saved, SavedLocal, Error };

    DataContext()
    {
        config = loadLocalConfig();
        manager.initialize();
        if (manager.isConnected()) {
            _initial_sync_started = true;
            if (hasPendingConfig()) {
                startSave(config);
            } else {
                startLoad();
            }
        }
    }

    ~DataContext() = default;

    bool shutdown()
    {
        _stopping.store(true);
        manager.cancelCurrentRequest();
        std::lock_guard<std::mutex> lifecycle_lock(_lifecycle_mutex);
        if (_task == nullptr && !_busy.load()) {
            return true;
        }
        // 请求尚在退出：Worker 立即放弃所有权，后台任务收尾后自释放，返回操作不等待网络超时。
        _abandoned.store(true);
        return false;
    }

    bool isBusy() const
    {
        return _busy.load();
    }

    void ensureInitialSync()
    {
        if (_initial_sync_started || !manager.isConnected()) {
            return;
        }
        _initial_sync_started = true;
        if (hasPendingConfig()) {
            startSave(config);
        } else {
            startLoad();
        }
    }

    void startLoad()
    {
        start(Operation::Load, config);
    }

    void startSave(const MarketDataConfig& value)
    {
        config = value;
        saveLocalConfig(config, true);
        if (!manager.isConnected()) {
            std::lock_guard<std::mutex> lock(_mutex);
            _result = Result::SavedLocal;
            _message = "SAVED LOCAL; SYNC AFTER WIFI";
            return;
        }
        start(Operation::Save, config);
    }

    bool takeResult(
        Result& result,
        MarketDataConfig& value,
        std::vector<std::string>& sector_values,
        std::string& message
    )
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_result == Result::None) {
            return false;
        }
        result = _result;
        value = config;
        sector_values = sectors;
        message = _message;
        _result = Result::None;
        return true;
    }

    MarketDataConfig snapshot()
    {
        std::lock_guard<std::mutex> lock(_mutex);
        return config;
    }

    std::vector<std::string> sectorSnapshot()
    {
        std::lock_guard<std::mutex> lock(_mutex);
        return sectors;
    }

    MarketDataConfig config;
    std::vector<std::string> sectors{"all"};
    stock_selector::NetManager manager;

private:
    enum class Operation { Load, Save };
    struct TaskRequest {
        DataContext* owner = nullptr;
        Operation operation = Operation::Load;
        MarketDataConfig config;
    };

    void start(Operation operation, const MarketDataConfig& value)
    {
        bool expected = false;
        if (!_busy.compare_exchange_strong(expected, true)) {
            return;
        }
        auto* request = new TaskRequest{this, operation, value};
        std::lock_guard<std::mutex> lifecycle_lock(_lifecycle_mutex);
        TaskHandle_t task = nullptr;
        const BaseType_t created = xTaskCreate(
            &DataContext::taskEntry,
            operation == Operation::Load ? "data_cfg_get" : "data_cfg_put",
            10 * 1024,
            request,
            3,
            &task
        );
        if (created != pdPASS) {
            delete request;
            _busy.store(false);
            std::lock_guard<std::mutex> lock(_mutex);
            _result = Result::Error;
            _message = "CANNOT START DATA TASK";
            return;
        }
        _task = task;
    }

    static void taskEntry(void* raw)
    {
        std::unique_ptr<TaskRequest> request(static_cast<TaskRequest*>(raw));
        DataContext* self = request->owner;
        std::string response;
        std::string error;
        bool success = false;
        MarketDataConfig parsed = request->config;
        std::vector<std::string> parsed_sectors{"all"};
        if (request->operation == Operation::Load) {
            success = self->manager.get("/api/v1/market-data/config", response, error) &&
                parseConfig(response, parsed, error);
            if (success) {
                std::string sectors_response;
                std::string sectors_error;
                if (self->manager.get(
                        "/api/v1/market-data/sectors?refresh=false",
                        sectors_response,
                        sectors_error
                    )) {
                    parseSectors(sectors_response, parsed_sectors);
                }
                if (parsed.stockSector != "all" &&
                    std::find(parsed_sectors.begin(), parsed_sectors.end(), parsed.stockSector) ==
                        parsed_sectors.end()) {
                    parsed_sectors.push_back(parsed.stockSector);
                }
            }
        } else {
            const std::string body = serializeConfig(request->config);
            success = self->manager.putJson(
                "/api/v1/market-data/config?refresh=true", body, response, error
            ) && parseConfig(response, parsed, error);
        }

        if (!self->_stopping.load()) {
            std::lock_guard<std::mutex> lock(self->_mutex);
            if (success) {
                self->config = parsed;
                if (request->operation == Operation::Load) {
                    self->sectors = std::move(parsed_sectors);
                }
                saveLocalConfig(parsed, false);
                self->_result = request->operation == Operation::Load ? Result::Loaded : Result::Saved;
                self->_message = request->operation == Operation::Load ?
                    "SERVER CONFIG LOADED" : "SAVED; REFRESH QUEUED";
            } else {
                self->_result = Result::Error;
                self->_message = error.empty() ? "DATA CONFIG FAILED" : error;
            }
        }
        bool abandoned = false;
        {
            std::lock_guard<std::mutex> lifecycle_lock(self->_lifecycle_mutex);
            self->_busy.store(false);
            self->_task = nullptr;
            abandoned = self->_abandoned.load();
        }
        if (abandoned) {
            delete self;
        }
        vTaskDelete(nullptr);
    }

    std::atomic<bool> _busy{false};
    std::atomic<bool> _stopping{false};
    std::atomic<bool> _abandoned{false};
    TaskHandle_t _task = nullptr;
    bool _initial_sync_started = false;
    std::mutex _mutex;
    std::mutex _lifecycle_mutex;
    Result _result = Result::None;
    std::string _message;
};

class DataSettingsWorker::DataSettingsView {
public:
    DataSettingsView()
    {
        _panel = std::make_unique<Container>(lv_screen_active());
        _panel->align(LV_ALIGN_CENTER, 0, 0);
        _panel->setSize(466, 466);
        _panel->setRadius(0);
        _panel->setBorderWidth(0);
        _panel->setPaddingAll(0);
        _panel->setBgColor(lv_color_hex(0x070B14));
        _panel->setBgOpa(LV_OPA_COVER);
        _panel->removeFlag(LV_OBJ_FLAG_SCROLLABLE);

        _title = makeLabel("MARKET DATA", &MontserratSemiBold26, 0xFFFFFF, LV_ALIGN_TOP_MID, 0, 27);
        _hint = makeLabel("KEY A/B: CHANGE VALUE", &lv_font_montserrat_10, 0x8D9AB0,
                          LV_ALIGN_TOP_MID, 0, 58);
        _field = makeLabel("TF 1D", &lv_font_montserrat_16, 0x8D9AB0, LV_ALIGN_TOP_MID, 0, 82);
        _position = makeLabel("1/26", &lv_font_montserrat_10, 0x65738A, LV_ALIGN_TOP_MID, 0, 105);

        _value_button = makeButton("ON", 72, 123, 322, 78, 0x121A2A);
        _value_button->label().setTextFont(&lv_font_montserrat_28);
        _value_button->onClick().connect([this]() { _next_field_requested = true; });

        _left_button = makeButton(LV_SYMBOL_LEFT, 49, 217, 104, 60, 0x243149);
        _left_button->onClick().connect([this]() { _step_requested = -1; });
        _right_button = makeButton(LV_SYMBOL_RIGHT, 313, 217, 104, 60, 0x243149);
        _right_button->onClick().connect([this]() { _step_requested = 1; });
        _change_hint = makeLabel("CHANGE", &lv_font_montserrat_14, 0x8D9AB0,
                                 LV_ALIGN_TOP_MID, 0, 239);

        _previous_field_button = makeButton("PREV FIELD", 86, 289, 143, 42, 0x192337);
        _previous_field_button->label().setTextFont(&lv_font_montserrat_14);
        _previous_field_button->onClick().connect([this]() { _field_move_requested = -1; });
        _next_field_button = makeButton("NEXT FIELD", 237, 289, 143, 42, 0x192337);
        _next_field_button->label().setTextFont(&lv_font_montserrat_14);
        _next_field_button->onClick().connect([this]() { _field_move_requested = 1; });

        _back_button = makeButton("BACK", 82, 342, 130, 60, 0x303A4D);
        _back_button->onClick().connect([this]() { _back_requested = true; });
        _apply_button = makeButton("APPLY + REFRESH", 220, 342, 164, 60, 0xFF8A34);
        _apply_button->label().setTextFont(&lv_font_montserrat_14);
        _apply_button->onClick().connect([this]() { _apply_requested = true; });

        _status = makeLabel("LOCAL CONFIG", &lv_font_montserrat_10, 0x8D9AB0,
                            LV_ALIGN_BOTTOM_MID, 0, -22);
        render();
    }

    void setConfig(const MarketDataConfig& config)
    {
        _config = config;
        render();
    }

    void setSectors(const std::vector<std::string>& sectors)
    {
        _sectors = sectors.empty() ? std::vector<std::string>{"all"} : sectors;
        if (std::find(_sectors.begin(), _sectors.end(), _config.stockSector) == _sectors.end()) {
            _sectors.push_back(_config.stockSector.empty() ? "all" : _config.stockSector);
        }
        render();
    }

    const MarketDataConfig& config() const
    {
        return _config;
    }

    void step(int direction)
    {
        if (direction == 0) {
            return;
        }
        switch (_field_index) {
            case 0: case 1: case 2: case 3: case 4: case 5: case 6: {
                const uint32_t bit = 1U << _field_index;
                _config.timeframeMask ^= bit;
                if (_config.timeframeMask == 0) _config.timeframeMask = bit;
                break;
            }
            case 7: cycle(_config.featureWindow, static_cast<int>(kWindows.size()), direction); break;
            case 8: cycle(_config.fetchBars, static_cast<int>(kFetchBars.size()), direction); break;
            case 9: _config.adaptive = !_config.adaptive; break;
            case 10: cycle(_config.refreshSeconds, static_cast<int>(kRefreshSeconds.size()), direction); break;
            case 11: _config.stockEnabled = !_config.stockEnabled; break;
            case 12: cycle(_config.stockCount, static_cast<int>(kCounts.size()), direction); break;
            case 13: cycle(_config.stockMetric, static_cast<int>(kStockMetrics.size()), direction); break;
            case 14: _config.stockTop = !_config.stockTop; break;
            case 15: stepSector(direction); break;
            case 16: _config.cryptoEnabled = !_config.cryptoEnabled; break;
            case 17: cycle(_config.cryptoCount, static_cast<int>(kCounts.size()), direction); break;
            case 18: cycle(_config.cryptoMetric, static_cast<int>(kCryptoMetrics.size()), direction); break;
            case 19: _config.cryptoTop = !_config.cryptoTop; break;
            case 20: cycle(_config.quoteAsset, static_cast<int>(kQuoteAssets.size()), direction); break;
            case 21: cycle(_config.binanceInterval, static_cast<int>(kBinanceIntervals.size()), direction); break;
            case 22: cycle(_config.binanceWeight, static_cast<int>(kBinanceWeights.size()), direction); break;
            case 23: cycle(_config.binanceConcurrency, static_cast<int>(kBinanceConcurrency.size()), direction); break;
            case 24: cycle(_config.binanceRetry, static_cast<int>(kBinanceRetries.size()), direction); break;
            case 25: cycle(_config.retryAfterSeconds, static_cast<int>(kRetryAfterSeconds.size()), direction); break;
            default: break;
        }
        render();
    }

    void nextField()
    {
        moveField(1);
    }

    void moveField(int direction)
    {
        _field_index = (_field_index + (direction > 0 ? 1 : -1) + kFieldCount) % kFieldCount;
        render();
    }

    int consumeStepRequested()
    {
        const int value = _step_requested;
        _step_requested = 0;
        return value;
    }

    bool consumeNextFieldRequested()
    {
        const bool value = _next_field_requested;
        _next_field_requested = false;
        return value;
    }

    int consumeFieldMoveRequested()
    {
        const int value = _field_move_requested;
        _field_move_requested = 0;
        return value;
    }

    bool consumeApplyRequested()
    {
        const bool value = _apply_requested;
        _apply_requested = false;
        return value;
    }

    bool consumeBackRequested()
    {
        const bool value = _back_requested;
        _back_requested = false;
        return value;
    }

    void setStatus(const std::string& text, bool error = false)
    {
        _status->setText(text.c_str());
        _status->setTextColor(lv_color_hex(error ? 0xFF667A : 0x31D0AA));
    }

    void setBusy(bool busy)
    {
        if (busy) {
            _apply_button->addState(LV_STATE_DISABLED);
        } else {
            _apply_button->removeState(LV_STATE_DISABLED);
        }
    }

private:
    static constexpr int kFieldCount = 26;

    static void cycle(int& value, int count, int direction)
    {
        value = (value + (direction > 0 ? 1 : -1) + count) % count;
    }

    void stepSector(int direction)
    {
        if (_sectors.empty()) _sectors.emplace_back("all");
        auto found = std::find(_sectors.begin(), _sectors.end(), _config.stockSector);
        int index = found == _sectors.end() ? 0 : static_cast<int>(found - _sectors.begin());
        cycle(index, static_cast<int>(_sectors.size()), direction);
        _config.stockSector = _sectors[index];
    }

    void render()
    {
        static constexpr const char* names[kFieldCount] = {
            "TF 1D", "TF 1W", "TF 4H", "TF 60M", "TF 30M", "TF 15M", "TF 5M",
            "FEATURE WINDOW", "FETCH BARS", "ADAPTIVE SCALE", "REFRESH CHECK",
            "STOCK", "STOCK COUNT", "STOCK RANK", "STOCK ORDER", "STOCK SECTOR",
            "CRYPTO", "CRYPTO COUNT", "CRYPTO RANK", "CRYPTO ORDER", "QUOTE ASSET",
            "REQUEST INTERVAL", "WEIGHT / MIN", "CONCURRENCY", "RETRY COUNT", "MAX RETRY AFTER"
        };
        std::string value;
        switch (_field_index) {
            case 0: case 1: case 2: case 3: case 4: case 5: case 6:
                value = (_config.timeframeMask & (1U << _field_index)) ? "SELECTED" : "OFF";
                break;
            case 7: value = std::to_string(kWindows[_config.featureWindow]); break;
            case 8: value = std::to_string(kFetchBars[_config.fetchBars]); break;
            case 9: value = _config.adaptive ? "ON" : "OFF"; break;
            case 10: value = std::to_string(kRefreshSeconds[_config.refreshSeconds]) + " SEC"; break;
            case 11: value = _config.stockEnabled ? "ENABLED" : "DISABLED"; break;
            case 12: value = kCounts[_config.stockCount] == 0 ? "ALL (0)" : std::to_string(kCounts[_config.stockCount]); break;
            case 13: value = kStockMetrics[_config.stockMetric]; break;
            case 14: value = _config.stockTop ? "TOP N" : "BOTTOM N"; break;
            case 15: {
                auto found = std::find(_sectors.begin(), _sectors.end(), _config.stockSector);
                const int sector_index = found == _sectors.end()
                    ? 0 : static_cast<int>(std::distance(_sectors.begin(), found));
                value = sector_index == 0
                    ? "ALL SECTORS"
                    : "CN SECTOR " + std::to_string(sector_index) + "/" +
                        std::to_string(std::max(1, static_cast<int>(_sectors.size()) - 1));
                break;
            }
            case 16: value = _config.cryptoEnabled ? "ENABLED" : "DISABLED"; break;
            case 17: value = kCounts[_config.cryptoCount] == 0 ? "ALL (0)" : std::to_string(kCounts[_config.cryptoCount]); break;
            case 18: value = kCryptoMetrics[_config.cryptoMetric]; break;
            case 19: value = _config.cryptoTop ? "TOP N" : "BOTTOM N"; break;
            case 20: value = kQuoteAssets[_config.quoteAsset]; break;
            case 21: value = std::to_string(kBinanceIntervals[_config.binanceInterval]) + " MS"; break;
            case 22: value = std::to_string(kBinanceWeights[_config.binanceWeight]); break;
            case 23: value = std::to_string(kBinanceConcurrency[_config.binanceConcurrency]); break;
            case 24: value = std::to_string(kBinanceRetries[_config.binanceRetry]); break;
            case 25: value = std::to_string(kRetryAfterSeconds[_config.retryAfterSeconds]) + " SEC"; break;
            default: value = "--"; break;
        }
        const std::string field_text = names[_field_index];
        if (field_text != _last_field_text) {
            _field->setText(field_text.c_str());
            _last_field_text = field_text;
        }
        if (value != _last_value_text) {
            _value_button->label().setTextFont(&lv_font_montserrat_28);
            _value_button->label().setText(value.c_str());
            _last_value_text = value;
        }
        char position[16] = {};
        std::snprintf(position, sizeof(position), "%d/%d", _field_index + 1, kFieldCount);
        if (_last_position_text != position) {
            _position->setText(position);
            _last_position_text = position;
        }
    }

    std::unique_ptr<Label> makeLabel(
        const char* text, const lv_font_t* font, uint32_t color, lv_align_t align, int x, int y
    )
    {
        auto label = std::make_unique<Label>(_panel->get());
        label->setText(text);
        label->setTextFont(font);
        label->setTextColor(lv_color_hex(color));
        label->align(align, x, y);
        return label;
    }

    std::unique_ptr<Button> makeButton(
        const char* text, int x, int y, int width, int height, uint32_t color
    )
    {
        auto button = std::make_unique<Button>(_panel->get());
        button->setSize(width, height);
        button->align(LV_ALIGN_TOP_LEFT, x, y);
        button->setRadius(std::min(height / 2, 24));
        button->setBorderWidth(0);
        button->setShadowWidth(0);
        button->setBgColor(lv_color_hex(color));
        button->label().setText(text);
        button->label().setTextFont(&lv_font_montserrat_18);
        button->label().setTextColor(lv_color_hex(0xFFFFFF));
        button->label().align(LV_ALIGN_CENTER, 0, 0);
        button->label().setWidth(width - 20);
        button->label().setTextAlign(LV_TEXT_ALIGN_CENTER);
        return button;
    }

    MarketDataConfig _config;
    std::vector<std::string> _sectors{"all"};
    int _field_index = 0;
    int _step_requested = 0;
    int _field_move_requested = 0;
    bool _next_field_requested = false;
    bool _apply_requested = false;
    bool _back_requested = false;
    std::string _last_field_text;
    std::string _last_value_text;
    std::string _last_position_text;
    std::unique_ptr<Container> _panel;
    std::unique_ptr<Label> _title;
    std::unique_ptr<Label> _hint;
    std::unique_ptr<Label> _field;
    std::unique_ptr<Label> _position;
    std::unique_ptr<Label> _change_hint;
    std::unique_ptr<Label> _status;
    std::unique_ptr<Button> _value_button;
    std::unique_ptr<Button> _left_button;
    std::unique_ptr<Button> _right_button;
    std::unique_ptr<Button> _previous_field_button;
    std::unique_ptr<Button> _next_field_button;
    std::unique_ptr<Button> _back_button;
    std::unique_ptr<Button> _apply_button;
};

DataSettingsWorker::DataSettingsWorker()
{
    _data = std::make_unique<DataContext>();
    _view = std::make_unique<DataSettingsView>();
    _view->setConfig(_data->snapshot());
    _view->setSectors(_data->sectorSnapshot());
    if (_data->isBusy()) {
        _view->setBusy(true);
        _view->setStatus(hasPendingConfig() ? "SYNCING SAVED CONFIG" : "LOADING SERVER CONFIG");
    } else if (!_data->manager.isConnected()) {
        _view->setStatus("WIFI OFFLINE; LOCAL EDIT MODE", true);
    }
}

void DataSettingsWorker::update()
{
    if (!_view || !_data) {
        return;
    }
    _data->ensureInitialSync();
    const int step = _view->consumeStepRequested();
    if (step != 0 && !_data->isBusy()) {
        _view->step(step);
    }
    if (_view->consumeNextFieldRequested() && !_data->isBusy()) {
        _view->nextField();
    }
    const int field_move = _view->consumeFieldMoveRequested();
    if (field_move != 0 && !_data->isBusy()) {
        _view->moveField(field_move);
    }
    if (_view->consumeBackRequested()) {
        _is_done = true;
        return;
    }
    if (_view->consumeApplyRequested() && !_data->isBusy()) {
        _data->startSave(_view->config());
        _view->setBusy(_data->isBusy());
        _view->setStatus(_data->isBusy() ? "SAVING + QUEUING REFRESH" : "SAVED LOCALLY");
    }

    DataContext::Result result;
    MarketDataConfig config;
    std::vector<std::string> sectors;
    std::string message;
    if (_data->takeResult(result, config, sectors, message)) {
        _view->setBusy(false);
        if (result == DataContext::Result::Loaded || result == DataContext::Result::Saved) {
            _view->setConfig(config);
            _view->setSectors(sectors);
            _view->setStatus(message);
        } else if (result == DataContext::Result::SavedLocal) {
            _view->setStatus(message, true);
        } else {
            _view->setStatus(message, true);
        }
    }
}

bool DataSettingsWorker::handleKey(input::KeyEvent event)
{
    if (!_view || !_data || _data->isBusy()) {
        return event == input::KeyEvent::GoPrevious || event == input::KeyEvent::GoNext;
    }
    if (event == input::KeyEvent::GoPrevious) {
        _view->step(-1);
        return true;
    }
    if (event == input::KeyEvent::GoNext) {
        _view->step(1);
        return true;
    }
    return false;
}

DataSettingsWorker::~DataSettingsWorker()
{
    if (_data && !_data->shutdown()) {
        _data.release();
    }
}

}  // namespace setup_workers
