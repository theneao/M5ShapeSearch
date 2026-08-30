/*
 * 创建时间：2026-08-28
 * 作用：封装 ESP-IDF WiFi 与 HTTP/1.1 请求，复用连接并把服务器地址保存到 NVS。
 * 修改时间：2026-08-29
 * 修改作用：首次启动写入默认 WiFi；补充整机设置页状态接口和市场数据配置所需 HTTP PUT。
 * 使用方式：先 initialize()；get/postJson/putJson 可在后台任务调用，活动请求可取消。
 * 修改时间：2026-08-29
 * 修改作用：HTTP 请求改用短连接，GET 响应头失败时在全新连接上重试一次。
 */
#pragma once

#include <esp_http_client.h>
#include <atomic>
#include <mutex>
#include <string>

namespace stock_selector {

class NetManager {
public:
    NetManager();
    ~NetManager();

    void initialize();
    bool isConnected() const;
    int rssi() const;
    std::string networkStatus() const;
    void startConfigPortal();
    void stopConfigPortal();
    bool isConfigPortalActive() const;
    std::string configPortalSsid() const;
    std::string connectedSsid() const;

    std::string baseUrl() const;
    void setBaseUrl(const std::string& url, bool persist = true);

    bool get(const std::string& path, std::string& response, std::string& error);
    bool postJson(
        const std::string& path,
        const std::string& body,
        std::string& response,
        std::string& error
    );
    bool putJson(
        const std::string& path,
        const std::string& body,
        std::string& response,
        std::string& error
    );
    bool cancelCurrentRequest();

    static std::string urlEncode(const std::string& value);

private:
    struct ResponseBuffer {
        std::string data;
        bool overflow = false;
    };

    static esp_err_t httpEventHandler(esp_http_client_event_t* event);
    bool perform(
        esp_http_client_method_t method,
        const std::string& path,
        const std::string* body,
        std::string& response,
        std::string& error
    );
    void ensureHttpClient(const std::string& url);

    mutable std::mutex _mutex;
    std::string _base_url;
    esp_http_client_handle_t _client = nullptr;
    std::atomic<esp_http_client_handle_t> _active_client{nullptr};
    ResponseBuffer _response;
    bool _wifi_started = false;
};

}  // namespace stock_selector
