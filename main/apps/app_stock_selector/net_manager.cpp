/*
 * 创建时间：2026-08-28
 * 作用：实现轻量网络层；形态计算完全留在 FastAPI 服务器。
 * 修改时间：2026-08-29
 * 修改作用：仅在 NVS 无 WiFi 时写默认值；支持数据设置页异步 PUT 服务端配置。
 * 使用方式：initialize() 后自动使用保存网络；数据页在后台调用 putJson()，不会阻塞 LVGL。
 *
 * 修改时间：2026-08-29
 * 修改作用：每次请求使用新 HTTP 连接，避免 FastAPI 已关闭的 keep-alive 连接导致详情 GET
 *           立即 ESP_ERR_HTTP_FETCH_HEADER；幂等 GET 遇连接/响应头错误自动重试一次。
 *
 * 修改时间：2026-08-29
 * 修改作用：WiFi 联网后自动通过 SNTP 校时，并把系统 UTC 写入硬件 RTC；不再依赖手工日期/时间设置。
 * 使用方式：任意 NetManager.initialize() 建立 STA 后自动启动，每小时由 LWIP 更新一次。
 *
 * 修改时间：2026-08-29
 * 修改作用：SNTP 初始化和 RTC I2C 写入迁移到独立 6KB 栈任务；系统事件/TCPIP 回调只设置原子标志，
 *           避免联网数秒后因小栈溢出或跨线程 I2C 竞争重启。
 *
 * 修改时间：2026-08-31
 * 修改作用：形态 POST 使用 60/75/90 秒的独立自适应读取超时，避免服务端完成全市场 NCC 后，
 *           设备仍按普通接口 15 秒超时误报失败；详情与设置请求继续使用短超时。
 * 使用方式：postJson("/api/v1/shape/match", ...) 自动采用匹配超时，无需调用方传参。
 */
#include "net_manager.h"

#include <hal/hal.h>
#include <hal/utils/settings/settings.h>
#include <ssid_manager.h>
#include <wifi_manager.h>

#if __has_include("private_config.h")
#include "private_config.h"
#else
#include "private_config.example.h"
#endif
#include <esp_log.h>
#include <esp_sntp.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <ctime>

#if CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
#include <esp_crt_bundle.h>
#endif

namespace stock_selector {
namespace {

constexpr const char* kTag = "ShapeNet";
constexpr const char* kSettingsNamespace = "shape_search";
constexpr const char* kServerUrlKey = "server_url";
constexpr const char* kDefaultServerUrl = CONFIG_SHAPE_SERVER_URL;
constexpr const char* kDefaultWifiSsid = M5_SHAPE_DEFAULT_WIFI_SSID;
constexpr const char* kDefaultWifiPassword = M5_SHAPE_DEFAULT_WIFI_PASSWORD;
constexpr std::size_t kMaxResponseBytes = 96 * 1024;
std::atomic<bool> g_time_task_started{false};
std::atomic<bool> g_time_sync_notified{false};

std::string trimTrailingSlash(std::string value)
{
    while (!value.empty() && value.back() == '/') {
        value.pop_back();
    }
    return value;
}

void timeSyncCallback(struct timeval*)
{
    // TCP/IP 任务栈很小，回调中禁止 NVS、日志格式化和 I2C 操作。
    g_time_sync_notified.store(true);
}

void networkTimeTask(void*)
{
    bool configured = false;
    while (true) {
        if (!configured && WifiManager::GetInstance().IsConnected()) {
            // POSIX TZ 中 CST-8 表示 UTC+8；系统内部与 RTC 仍保存 UTC。
            GetHAL().setTimezone("CST-8");
            esp_sntp_set_time_sync_notification_cb(&timeSyncCallback);
            if (!esp_sntp_enabled()) {
                esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
                esp_sntp_setservername(0, "ntp.aliyun.com");
                esp_sntp_init();
            }
            configured = true;
            ESP_LOGI(kTag, "SNTP automatic time sync started in worker task");
        }
        if (g_time_sync_notified.exchange(false)) {
            GetHAL().syncSystemTimeToRtc();
            const std::time_t now = std::time(nullptr);
            ESP_LOGI(kTag, "Network time synchronized and saved to RTC, utc=%lld",
                     static_cast<long long>(now));
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void scheduleNetworkTimeSync()
{
    bool expected = false;
    if (!g_time_task_started.compare_exchange_strong(expected, true)) {
        return;
    }
    if (xTaskCreate(&networkTimeTask, "network_time", 6 * 1024, nullptr, 2, nullptr) != pdPASS) {
        g_time_task_started.store(false);
        ESP_LOGE(kTag, "Cannot create network time task");
    }
}

}  // namespace

NetManager::NetManager()
{
    Settings settings(kSettingsNamespace, false);
    _base_url = trimTrailingSlash(settings.GetString(kServerUrlKey, kDefaultServerUrl));
}

NetManager::~NetManager()
{
    std::lock_guard<std::mutex> lock(_mutex);
    if (_client != nullptr) {
        esp_http_client_cleanup(_client);
        _client = nullptr;
    }
}

void NetManager::initialize()
{
    std::lock_guard<std::mutex> lock(_mutex);
    auto& wifi = WifiManager::GetInstance();
    WifiManagerConfig config;
    config.ssid_prefix = "M5Shape";
    config.language = "zh-CN";
    if (!wifi.IsInitialized() && !wifi.Initialize(config)) {
        ESP_LOGE(kTag, "WiFi manager initialize failed");
        return;
    }
    if (!_wifi_started) {
        wifi.SetEventCallback([](WifiEvent event, const std::string& data) {
            if (event == WifiEvent::ConfigModeExit) {
                // stopConfigPortal() 会显式恢复 STA；回调内不得递归启动 WiFi。
                ESP_LOGI(kTag, "WiFi configuration mode exited");
            } else if (event == WifiEvent::Connected) {
                ESP_LOGI(kTag, "WiFi connected: %s", data.c_str());
            } else if (event == WifiEvent::Disconnected) {
                ESP_LOGW(kTag, "WiFi disconnected: %s", data.c_str());
            }
        });
    }
    if (wifi.IsConfigMode()) {
        _wifi_started = true;
        return;
    }
    // 其他 App 的配置热点可能直接切换过 ESP WiFi 模式；重新打开本 App 时显式恢复 STA。
    if (_wifi_started) {
        wifi.StopStation();
    }
    auto& ssid_manager = SsidManager::GetInstance();
    if (ssid_manager.GetSsidList().empty() && kDefaultWifiSsid[0] != '\0') {
        // 只在真正的首次启动写入默认值。之后配网页面写入的列表由 NVS 恢复，绝不被默认值覆盖。
        ssid_manager.AddSsid(kDefaultWifiSsid, kDefaultWifiPassword);
        ESP_LOGI(kTag, "No saved WiFi; persisted factory default SSID: %s", kDefaultWifiSsid);
    }
    wifi.StartStation();
    _wifi_started = true;
    scheduleNetworkTimeSync();
    ESP_LOGI(kTag, "WiFi station started, API=%s", _base_url.c_str());
}

bool NetManager::isConnected() const
{
    return WifiManager::GetInstance().IsConnected();
}

int NetManager::rssi() const
{
    return isConnected() ? WifiManager::GetInstance().GetRssi() : -127;
}

std::string NetManager::networkStatus() const
{
    auto& wifi = WifiManager::GetInstance();
    if (wifi.IsConnected()) {
        return "ONLINE " + wifi.GetIpAddress();
    }
    if (wifi.IsConfigMode()) {
        return "SETUP " + wifi.GetApSsid();
    }
    return _wifi_started ? "CONNECTING" : "OFFLINE";
}

void NetManager::startConfigPortal()
{
    auto& wifi = WifiManager::GetInstance();
    if (!wifi.IsInitialized()) {
        initialize();
    }
    if (!wifi.IsConfigMode()) {
        wifi.StartConfigAp();
    }
}

void NetManager::stopConfigPortal()
{
    auto& wifi = WifiManager::GetInstance();
    if (wifi.IsConfigMode()) {
        wifi.StopConfigAp();
    }
    if (!wifi.IsConnected()) {
        wifi.StartStation();
    }
}

bool NetManager::isConfigPortalActive() const
{
    return WifiManager::GetInstance().IsConfigMode();
}

std::string NetManager::configPortalSsid() const
{
    return WifiManager::GetInstance().GetApSsid();
}

std::string NetManager::connectedSsid() const
{
    return WifiManager::GetInstance().GetSsid();
}

std::string NetManager::baseUrl() const
{
    std::string fallback;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        fallback = _base_url;
    }
    // 配网页面可能在本对象存活期间更新 NVS；状态页每次读取即可立即显示新地址。
    Settings settings(kSettingsNamespace, false);
    return trimTrailingSlash(settings.GetString(kServerUrlKey, fallback));
}

void NetManager::setBaseUrl(const std::string& url, bool persist)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _base_url = trimTrailingSlash(url);
    if (persist) {
        Settings settings(kSettingsNamespace, true);
        settings.SetString(kServerUrlKey, _base_url);
    }
    if (_client != nullptr) {
        esp_http_client_cleanup(_client);
        _client = nullptr;
    }
}

esp_err_t NetManager::httpEventHandler(esp_http_client_event_t* event)
{
    auto* output = static_cast<ResponseBuffer*>(event->user_data);
    if (output == nullptr) {
        return ESP_OK;
    }
    if (event->event_id == HTTP_EVENT_ON_DATA && event->data != nullptr && event->data_len > 0) {
        const std::size_t new_size = output->data.size() + static_cast<std::size_t>(event->data_len);
        if (new_size > kMaxResponseBytes) {
            output->overflow = true;
            return ESP_FAIL;
        }
        output->data.append(static_cast<const char*>(event->data), static_cast<std::size_t>(event->data_len));
    }
    return ESP_OK;
}

void NetManager::ensureHttpClient(const std::string& url)
{
    if (_client != nullptr) {
        return;
    }
    esp_http_client_config_t config = {};
    config.url = url.c_str();
    config.event_handler = &NetManager::httpEventHandler;
    config.user_data = &_response;
    config.timeout_ms = 15000;
    config.buffer_size = 4096;
    config.buffer_size_tx = 4096;
    // FastAPI/代理的 keep-alive 空闲时限可能短于用户查看结果的时间；不复用陈旧 socket。
    config.keep_alive_enable = false;
#if CONFIG_MBEDTLS_CERTIFICATE_BUNDLE
    config.crt_bundle_attach = esp_crt_bundle_attach;
#endif
    _client = esp_http_client_init(&config);
}

bool NetManager::perform(
    esp_http_client_method_t method,
    const std::string& path,
    const std::string* body,
    std::string& response,
    std::string& error
)
{
    std::lock_guard<std::mutex> lock(_mutex);
    if (!WifiManager::GetInstance().IsConnected()) {
        error = "WiFi is not connected";
        return false;
    }

    // 配网门户可直接修改 NVS；每次请求前重读，无需重启 App。
    Settings settings(kSettingsNamespace, false);
    const std::string configured_url = trimTrailingSlash(
        settings.GetString(kServerUrlKey, kDefaultServerUrl)
    );
    if (!configured_url.empty() && configured_url != _base_url) {
        _base_url = configured_url;
        if (_client != nullptr) {
            esp_http_client_cleanup(_client);
            _client = nullptr;
        }
        ESP_LOGI(kTag, "Reloaded API URL from NVS: %s", _base_url.c_str());
    }

    const std::string url = _base_url + path;
    const int signal = WifiManager::GetInstance().GetRssi();
    const bool shape_match = method == HTTP_METHOD_POST && path == "/api/v1/shape/match";
    const int socket_timeout_ms = shape_match
        ? (signal <= -80 ? 90000 : signal <= -70 ? 75000 : 60000)
        : (signal <= -80 ? 30000 : signal <= -70 ? 22000 : 15000);
    const int64_t started = esp_timer_get_time();
    esp_err_t result = ESP_FAIL;
    int status = -1;
    const int attempts = method == HTTP_METHOD_GET ? 2 : 1;
    for (int attempt = 0; attempt < attempts; ++attempt) {
        ensureHttpClient(url);
        if (_client == nullptr) {
            error = "HTTP client init failed";
            return false;
        }

        _response.data.clear();
        _response.overflow = false;
        esp_http_client_set_url(_client, url.c_str());
        esp_http_client_set_method(_client, method);
        esp_http_client_set_timeout_ms(_client, socket_timeout_ms);
        esp_http_client_set_header(_client, "Accept", "application/json");
        esp_http_client_set_header(_client, "Accept-Encoding", "identity");
        esp_http_client_set_header(_client, "Connection", "close");
        if (body != nullptr) {
            esp_http_client_set_header(_client, "Content-Type", "application/json; charset=utf-8");
            esp_http_client_set_post_field(_client, body->c_str(), static_cast<int>(body->size()));
        } else {
            esp_http_client_set_post_field(_client, nullptr, 0);
        }

        _active_client.store(_client);
        result = esp_http_client_perform(_client);
        _active_client.store(nullptr);
        status = esp_http_client_get_status_code(_client);
        esp_http_client_cleanup(_client);
        _client = nullptr;

        const bool retryable = !_response.overflow && attempt + 1 < attempts &&
            (result == ESP_ERR_HTTP_FETCH_HEADER || result == ESP_ERR_HTTP_CONNECT ||
             result == ESP_ERR_HTTP_READ_TIMEOUT || result == ESP_ERR_TIMEOUT);
        if (!retryable) {
            break;
        }
        ESP_LOGW(kTag, "GET response failed (%s); retry with a fresh connection",
                 esp_err_to_name(result));
    }
    const int elapsed_ms = static_cast<int>((esp_timer_get_time() - started) / 1000);
    const char* method_name = method == HTTP_METHOD_POST ? "POST" :
        method == HTTP_METHOD_PUT ? "PUT" : "GET";
    ESP_LOGI(kTag, "%s %s -> HTTP %d, %u bytes, %d ms",
             method_name, path.c_str(), status,
             static_cast<unsigned>(_response.data.size()), elapsed_ms);

    if (result != ESP_OK) {
        if (result == ESP_ERR_HTTP_CONNECT) {
            error = "Cannot reach server; check IP, port and firewall";
        } else if (result == ESP_ERR_HTTP_READ_TIMEOUT || result == ESP_ERR_TIMEOUT) {
            error = "Server response timed out";
        } else {
            error = std::string("HTTP request failed: ") + esp_err_to_name(result);
        }
        return false;
    }
    if (_response.overflow) {
        error = "Server response is too large";
        return false;
    }
    if (status < 200 || status >= 300) {
        error = "Server returned HTTP " + std::to_string(status);
        if (!_response.data.empty()) {
            error += ": " + _response.data.substr(0, 120);
        }
        return false;
    }
    response = _response.data;
    return true;
}

bool NetManager::cancelCurrentRequest()
{
    esp_http_client_handle_t client = _active_client.load();
    if (client == nullptr) {
        return false;
    }
    const esp_err_t result = esp_http_client_cancel_request(client);
    ESP_LOGW(kTag, "Cancel active HTTP request: %s", esp_err_to_name(result));
    return result == ESP_OK;
}

bool NetManager::get(const std::string& path, std::string& response, std::string& error)
{
    return perform(HTTP_METHOD_GET, path, nullptr, response, error);
}

bool NetManager::postJson(
    const std::string& path,
    const std::string& body,
    std::string& response,
    std::string& error
)
{
    return perform(HTTP_METHOD_POST, path, &body, response, error);
}

bool NetManager::putJson(
    const std::string& path,
    const std::string& body,
    std::string& response,
    std::string& error
)
{
    return perform(HTTP_METHOD_PUT, path, &body, response, error);
}

std::string NetManager::urlEncode(const std::string& value)
{
    std::string encoded;
    encoded.reserve(value.size() * 3);
    const char* hex = "0123456789ABCDEF";
    for (const unsigned char ch : value) {
        if (std::isalnum(ch) || ch == '-' || ch == '_' || ch == '.' || ch == '~') {
            encoded.push_back(static_cast<char>(ch));
        } else {
            encoded.push_back('%');
            encoded.push_back(hex[(ch >> 4) & 0x0F]);
            encoded.push_back(hex[ch & 0x0F]);
        }
    }
    return encoded;
}

}  // namespace stock_selector
