/*
 * 创建时间：2026-08-29
 * 修改时间：2026-08-29
 * 作用：整机 Settings 的独立 WiFi/服务器页，显示信号与 Shape API，并启动可持久化的配网门户。
 * 使用方式：进入 Settings -> WiFi / Server；B 或触屏开启/退出 M5Shape-* 热点，A 键或左滑返回。
 *
 * 修改时间：2026-08-29
 * 修改作用：配置热点明确为默认关闭；新增异步服务器看板，显示状态、进度及各周期 Stock/Crypto 数量。
 *
 * 修改时间：2026-08-31
 * 修改作用：服务器看板改用 compact 状态接口，首次请求失败时用 -- 而不是误导性的全 0；
 *           成功后若网络短暂失败则保留最后一次真实数量，并降低自动轮询频率。
 * 使用方式：进入 Settings -> Server Dashboard；B 立即刷新，页面每 10 秒自动刷新一次。
 */
#include "workers.h"

#include <apps/app_stock_selector/net_manager.h>
#include <assets/assets.h>
#include <hal/hal.h>
#include <ArduinoJson.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <array>
#include <atomic>
#include <cstdio>
#include <mutex>

using namespace smooth_ui_toolkit::lvgl_cpp;
using namespace setup_workers;

namespace setup_workers {
class WifiSettingsWorker::NetworkContext {
public:
    stock_selector::NetManager manager;
};

class WifiSettingsWorker::WifiSettingsView {
public:
    WifiSettingsView()
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

        _title = makeLabel("WIFI & SERVER", &MontserratSemiBold26, 0xFFFFFF, LV_ALIGN_TOP_MID, 0, 34);
        _back_hint = makeLabel("KEY A / SWIPE LEFT: BACK", &lv_font_montserrat_10, 0x8D9AB0,
                               LV_ALIGN_TOP_MID, 0, 68);

        _card = std::make_unique<Container>(_panel->get());
        _card->setSize(374, 160);
        _card->align(LV_ALIGN_TOP_MID, 0, 91);
        _card->setBgColor(lv_color_hex(0x121A2A));
        _card->setBorderWidth(0);
        _card->setRadius(26);
        _card->setPaddingAll(0);
        _card->removeFlag(LV_OBJ_FLAG_SCROLLABLE);

        _wifi_icon = makeCardLabel(LV_SYMBOL_WIFI, &lv_font_montserrat_22, 0x8D9AB0, 24, 18);
        _status = makeCardLabel("CONNECTING", &lv_font_montserrat_16, 0xFFFFFF, 62, 20);
        _ssid = makeCardLabel("SSID: --", &lv_font_montserrat_14, 0x8D9AB0, 24, 59);
        _api = makeCardLabel("API: --", &lv_font_montserrat_14, 0x8D9AB0, 24, 96);

        _toggle_button = std::make_unique<Button>(_panel->get());
        _toggle_button->setSize(330, 78);
        _toggle_button->align(LV_ALIGN_TOP_MID, 0, 301);
        _toggle_button->setRadius(39);
        _toggle_button->setBorderWidth(0);
        _toggle_button->setShadowWidth(0);
        _toggle_button->setBgColor(lv_color_hex(0xFF8A34));
        _toggle_button->label().setText("SETUP HOTSPOT: OFF");
        _toggle_button->label().setTextFont(&lv_font_montserrat_16);
        _toggle_button->label().setTextColor(lv_color_hex(0xFFFFFF));
        _toggle_button->label().align(LV_ALIGN_CENTER, 0, 0);
        _toggle_button->label().setWidth(286);
        _toggle_button->label().setTextAlign(LV_TEXT_ALIGN_CENTER);
        _toggle_button->onClick().connect([this]() { _toggle_requested = true; });

        _help = makeLabel("Phone setup only - default OFF", &lv_font_montserrat_14, 0x8D9AB0,
                          LV_ALIGN_BOTTOM_MID, 0, -56);
    }

    bool consumeToggleRequested()
    {
        const bool requested = _toggle_requested;
        _toggle_requested = false;
        return requested;
    }

    void setState(
        const std::string& status,
        const std::string& ssid,
        const std::string& api_url,
        int rssi,
        bool portal_active,
        const std::string& portal_ssid
    )
    {
        char status_text[96] = {};
        if (rssi > -120) {
            std::snprintf(status_text, sizeof(status_text), "%s  %d dBm", status.c_str(), rssi);
        } else {
            std::snprintf(status_text, sizeof(status_text), "%s", status.c_str());
        }
        _status->setText(status_text);
        _status->setTextColor(lv_color_hex(rssi <= -120 ? 0x8D9AB0 : rssi <= -80 ? 0xFF667A :
                                           rssi <= -70 ? 0xFF8A34 : 0x31D0AA));
        _wifi_icon->setText(portal_active || rssi > -120 ? LV_SYMBOL_WIFI : LV_SYMBOL_CLOSE);
        _wifi_icon->setTextColor(lv_color_hex(portal_active ? 0x3C82F6 : rssi <= -120 ? 0x8D9AB0 :
                                              rssi <= -80 ? 0xFF667A : rssi <= -70 ? 0xFF8A34 : 0x31D0AA));

        _ssid->setText((portal_active ? "SETUP AP: " + portal_ssid : "SSID: " +
                        (ssid.empty() ? std::string("CONNECTING") : ssid)).c_str());
        _api->setText(("API: " + api_url).c_str());
        _toggle_button->label().setText(portal_active ? "SETUP HOTSPOT: ON" :
                                                        "SETUP HOTSPOT: OFF");
        _help->setText(portal_active ? "Connect phone; open 192.168.4.1" :
                                      "Phone setup only - default OFF");
    }

private:
    std::unique_ptr<Label> makeLabel(
        const char* text,
        const lv_font_t* font,
        uint32_t color,
        lv_align_t align,
        int x,
        int y
    )
    {
        auto label = std::make_unique<Label>(_panel->get());
        label->setText(text);
        label->setTextFont(font);
        label->setTextColor(lv_color_hex(color));
        label->align(align, x, y);
        return label;
    }

    std::unique_ptr<Label> makeCardLabel(
        const char* text,
        const lv_font_t* font,
        uint32_t color,
        int x,
        int y
    )
    {
        auto label = std::make_unique<Label>(_card->get());
        label->setText(text);
        label->setTextFont(font);
        label->setTextColor(lv_color_hex(color));
        label->align(LV_ALIGN_TOP_LEFT, x, y);
        label->setWidth(326 - x);
        label->setLongMode(LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
        return label;
    }

    std::unique_ptr<Container> _panel;
    std::unique_ptr<Container> _card;
    std::unique_ptr<Label> _title;
    std::unique_ptr<Label> _back_hint;
    std::unique_ptr<Label> _wifi_icon;
    std::unique_ptr<Label> _status;
    std::unique_ptr<Label> _ssid;
    std::unique_ptr<Label> _api;
    std::unique_ptr<Label> _help;
    std::unique_ptr<Button> _toggle_button;
    bool _toggle_requested = false;
};

WifiSettingsWorker::WifiSettingsWorker()
{
    _network = std::make_unique<NetworkContext>();
    _network->manager.initialize();
    _view = std::make_unique<WifiSettingsView>();
    _next_refresh_tick = 0;
}

void WifiSettingsWorker::update()
{
    if (!_network || !_view) {
        return;
    }
    if (_view->consumeToggleRequested()) {
        if (_network->manager.isConfigPortalActive()) {
            _network->manager.stopConfigPortal();
        } else {
            _network->manager.startConfigPortal();
        }
        _next_refresh_tick = 0;
    }

    const uint32_t now = GetHAL().millis();
    if (now < _next_refresh_tick) {
        return;
    }
    _next_refresh_tick = now + 500;
    _view->setState(
        _network->manager.networkStatus(),
        _network->manager.connectedSsid(),
        _network->manager.baseUrl(),
        _network->manager.rssi(),
        _network->manager.isConfigPortalActive(),
        _network->manager.configPortalSsid()
    );
}

bool WifiSettingsWorker::handleKey(input::KeyEvent event)
{
    if (event != input::KeyEvent::GoNext || !_network) {
        return false;
    }
    if (_network->manager.isConfigPortalActive()) {
        _network->manager.stopConfigPortal();
    } else {
        _network->manager.startConfigPortal();
    }
    _next_refresh_tick = 0;
    return true;
}

WifiSettingsWorker::~WifiSettingsWorker()
{
    // A 键/左滑表示取消当前设置；若热点仍在运行，恢复 STA，避免离开页面后设备一直离线。
    if (_network && _network->manager.isConfigPortalActive()) {
        _network->manager.stopConfigPortal();
    }
}

class ServerDashboardWorker::DashboardContext :
    public std::enable_shared_from_this<ServerDashboardWorker::DashboardContext> {
public:
    struct Snapshot {
        std::string state = "CONNECTING";
        std::string message;
        bool has_data = false;
        int progress = 0;
        int total = 0;
        std::array<int, 7> stocks{};
        std::array<int, 7> cryptos{};
    };

    DashboardContext()
    {
        manager.initialize();
    }

    void request()
    {
        bool expected = false;
        if (!_busy.compare_exchange_strong(expected, true)) {
            return;
        }
        auto* holder = new std::shared_ptr<DashboardContext>(shared_from_this());
        if (xTaskCreate(&DashboardContext::taskEntry, "server_dashboard", 10 * 1024,
                        holder, 3, nullptr) != pdPASS) {
            delete holder;
            _busy.store(false);
            setError("CANNOT START STATUS TASK");
        }
    }

    bool take(Snapshot& value)
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_version == _taken_version) {
            return false;
        }
        value = _snapshot;
        _taken_version = _version;
        return true;
    }

private:
    static void taskEntry(void* raw)
    {
        std::unique_ptr<std::shared_ptr<DashboardContext>> holder(
            static_cast<std::shared_ptr<DashboardContext>*>(raw)
        );
        const auto self = *holder;
        std::string response;
        std::string error;
        if (!self->manager.get("/api/v1/market-data/status?compact=true", response, error)) {
            self->setError(error.empty() ? "STATUS REQUEST FAILED" : error);
        } else {
            self->parse(response);
        }
        self->_busy.store(false);
        vTaskDelete(nullptr);
    }

    void parse(const std::string& response)
    {
        JsonDocument document;
        const DeserializationError parse_error = deserializeJson(document, response);
        if (parse_error || document["code"].as<int>() != 0) {
            setError(parse_error ? "INVALID STATUS JSON" : "SERVER REJECTED STATUS");
            return;
        }
        static constexpr const char* kTf[] = {"1d", "1w", "4h", "60m", "30m", "15m", "5m"};
        Snapshot next;
        JsonObjectConst data = document["data"];
        JsonObjectConst buckets = data["buckets"];
        if (data.isNull() || buckets.isNull()) {
            setError("STATUS HAS NO BUCKET DATA");
            return;
        }
        next.has_data = true;
        next.state = data["state"] | "unknown";
        next.message = data["message"] | "";
        next.progress = static_cast<int>((data["progress"] | 0.0f) * 100.0f + 0.5f);
        for (int index = 0; index < 7; ++index) {
            const std::string stock_key = std::string("stock_") + kTf[index];
            const std::string crypto_key = std::string("crypto_") + kTf[index];
            next.stocks[index] = buckets[stock_key] | 0;
            next.cryptos[index] = buckets[crypto_key] | 0;
            next.total += next.stocks[index] + next.cryptos[index];
        }
        std::lock_guard<std::mutex> lock(_mutex);
        _snapshot = std::move(next);
        ++_version;
    }

    void setError(const std::string& message)
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _snapshot.state = "ERROR";
        _snapshot.message = message;
        ++_version;
    }

    stock_selector::NetManager manager;
    std::atomic<bool> _busy{false};
    std::mutex _mutex;
    Snapshot _snapshot;
    uint32_t _version = 0;
    uint32_t _taken_version = 0;
};

class ServerDashboardWorker::DashboardView {
public:
    DashboardView()
    {
        _panel = std::make_unique<Container>(lv_screen_active());
        _panel->setSize(466, 466);
        _panel->setBgColor(lv_color_hex(0x070B14));
        _panel->setBorderWidth(0);
        _panel->setRadius(0);
        _panel->setPaddingAll(0);
        _panel->removeFlag(LV_OBJ_FLAG_SCROLLABLE);

        _title = label("SERVER DASHBOARD", &MontserratSemiBold26, 0xFFFFFF, 0, 28);
        _state = label("LOADING", &lv_font_montserrat_14, 0xFF8A34, 0, 65);
        _total = label("TOTAL SERIES --", &lv_font_montserrat_14, 0x8D9AB0, 0, 88);
        static constexpr const char* kTf[] = {"1D", "1W", "4H", "60M", "30M", "15M", "5M"};
        for (int index = 0; index < 7; ++index) {
            _rows[index] = label(kTf[index], &lv_font_montserrat_16, 0xFFFFFF, 0, 120 + index * 37);
        }
        _hint = label("KEY B: REFRESH    KEY A: BACK", &lv_font_montserrat_10, 0x65738A, 0, 394);
    }

    void render(const DashboardContext::Snapshot& value)
    {
        char text[128] = {};
        std::snprintf(text, sizeof(text), "%s  %d%%", value.state.c_str(), value.progress);
        _state->setText(text);
        _state->setTextColor(lv_color_hex(value.state == "ERROR" ? 0xFF667A :
                                          value.state == "refreshing" ? 0xFF8A34 : 0x31D0AA));
        if (value.has_data) {
            std::snprintf(text, sizeof(text), "TOTAL SERIES %d", value.total);
        } else {
            std::snprintf(text, sizeof(text), "TOTAL SERIES --");
        }
        _total->setText(text);
        static constexpr const char* kTf[] = {"1D", "1W", "4H", "60M", "30M", "15M", "5M"};
        for (int index = 0; index < 7; ++index) {
            if (value.has_data) {
                std::snprintf(text, sizeof(text), "%s    STOCK %d    CRYPTO %d",
                              kTf[index], value.stocks[index], value.cryptos[index]);
            } else {
                std::snprintf(text, sizeof(text), "%s    STOCK --    CRYPTO --", kTf[index]);
            }
            _rows[index]->setText(text);
        }
        _hint->setText(value.state == "ERROR" ? value.message.c_str() :
                       "KEY B: REFRESH    KEY A: BACK");
    }

private:
    std::unique_ptr<Label> label(const char* text, const lv_font_t* font, uint32_t color, int x, int y)
    {
        auto item = std::make_unique<Label>(_panel->get());
        item->setText(text);
        item->setTextFont(font);
        item->setTextColor(lv_color_hex(color));
        item->align(LV_ALIGN_TOP_MID, x, y);
        return item;
    }

    std::unique_ptr<Container> _panel;
    std::unique_ptr<Label> _title;
    std::unique_ptr<Label> _state;
    std::unique_ptr<Label> _total;
    std::array<std::unique_ptr<Label>, 7> _rows;
    std::unique_ptr<Label> _hint;
};

ServerDashboardWorker::ServerDashboardWorker()
{
    _dashboard = std::make_shared<DashboardContext>();
    _view = std::make_unique<DashboardView>();
    _dashboard->request();
    _next_refresh_tick = GetHAL().millis() + 10000;
}

void ServerDashboardWorker::update()
{
    if (!_dashboard || !_view) {
        return;
    }
    DashboardContext::Snapshot value;
    if (_dashboard->take(value)) {
        _view->render(value);
    }
    const uint32_t now = GetHAL().millis();
    if (now >= _next_refresh_tick) {
        _dashboard->request();
        _next_refresh_tick = now + 10000;
    }
}

bool ServerDashboardWorker::handleKey(input::KeyEvent event)
{
    if (event != input::KeyEvent::GoNext || !_dashboard) {
        return false;
    }
    _dashboard->request();
    _next_refresh_tick = GetHAL().millis() + 10000;
    return true;
}

ServerDashboardWorker::~ServerDashboardWorker()
{
    _view.reset();
    _dashboard.reset();
}

}  // namespace setup_workers
