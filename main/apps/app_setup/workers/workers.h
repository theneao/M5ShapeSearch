/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：WiFi/服务器与市场数据拆为独立设置页；工作页可按需接管 A/B 键。
 * 使用方式：AppSetup 创建对应 Worker；Data 页用 A/B 左右选择，触屏确认和返回。
 *
 * 修改时间：2026-08-29
 * 修改作用：新增动画耗电档位与服务器缓存看板 Worker；时间改为联网自动同步。
 */
#pragma once
#include <smooth_lvgl.hpp>
#include <uitk/short_namespace.hpp>
#include <hal/hal.h>
#include <apps/common/key_manager/key_manager.h>
#include <cstdint>
#include <memory>
#include <string_view>

namespace setup_workers {

class PercentageAdjustView;

/**
 * @brief
 *
 */
class WorkerBase {
public:
    virtual ~WorkerBase() = default;

    virtual void update()
    {
    }

    virtual bool handleKey(input::KeyEvent event)
    {
        (void)event;
        return false;
    }

    bool isDone() const
    {
        return _is_done;
    }

protected:
    bool _is_done = false;
};

/**
 * @brief
 *
 */
class BrightnessWorker : public WorkerBase {
public:
    BrightnessWorker();
    ~BrightnessWorker();
    void update() override;

private:
    std::unique_ptr<PercentageAdjustView> _view;
    int _applied_brightness = 0;
    bool _save_requested    = false;
};

/**
 * @brief
 *
 */
class VolumeWorker : public WorkerBase {
public:
    VolumeWorker();
    ~VolumeWorker();
    void update() override;

private:
    std::unique_ptr<PercentageAdjustView> _view;
    int _applied_volume  = 0;
    bool _save_requested = false;
};

/**
 * @brief
 *
 */
class ButtonWorker : public WorkerBase {
public:
    ButtonWorker();
    ~ButtonWorker();
    void update() override;

private:
    class ButtonConfigView;

    std::unique_ptr<ButtonConfigView> _view;
    Hal::ButtonConfig _applied_config;
};

class MotionWorker : public WorkerBase {
public:
    MotionWorker();
    ~MotionWorker();
    void update() override;
    bool handleKey(input::KeyEvent event) override;

private:
    class MotionConfigView;
    std::unique_ptr<MotionConfigView> _view;
};

/**
 * @brief
 *
 */
class SetTimeWorker : public WorkerBase {
public:
    SetTimeWorker();
    ~SetTimeWorker();
    void update() override;

private:
    class TimeAdjustView;

    std::unique_ptr<TimeAdjustView> _view;
    TimeHms _applied_time;
};

/**
 * @brief
 *
 */
class SetDateWorker : public WorkerBase {
public:
    SetDateWorker();
    ~SetDateWorker();
    void update() override;

private:
    class DateAdjustView;

    std::unique_ptr<DateAdjustView> _view;
    DateYmd _applied_date;
};

/**
 * @brief
 *
 */
class AboutWorker : public WorkerBase {
public:
    AboutWorker();
    ~AboutWorker();
    void update() override;

private:
    class AboutView;

    std::unique_ptr<AboutView> _view;
    int _progress                = 0;
    uint32_t _next_progress_tick = 0;
    int _pending_burst_steps     = 0;
};

/**
 * @brief 整机 WiFi 与 Shape API 设置入口
 */
class WifiSettingsWorker : public WorkerBase {
public:
    WifiSettingsWorker();
    ~WifiSettingsWorker();
    void update() override;
    bool handleKey(input::KeyEvent event) override;

private:
    class WifiSettingsView;
    class NetworkContext;

    std::unique_ptr<WifiSettingsView> _view;
    std::unique_ptr<NetworkContext> _network;
    uint32_t _next_refresh_tick = 0;
};

/**
 * @brief 市场数据直接配置页；A/B 调值，触屏切字段、保存和返回
 */
class DataSettingsWorker : public WorkerBase {
public:
    DataSettingsWorker();
    ~DataSettingsWorker();
    void update() override;
    bool handleKey(input::KeyEvent event) override;

private:
    class DataSettingsView;
    class DataContext;

    std::unique_ptr<DataSettingsView> _view;
    std::unique_ptr<DataContext> _data;
};

class ServerDashboardWorker : public WorkerBase {
public:
    ServerDashboardWorker();
    ~ServerDashboardWorker();
    void update() override;
    bool handleKey(input::KeyEvent event) override;

private:
    class DashboardView;
    class DashboardContext;
    std::unique_ptr<DashboardView> _view;
    std::shared_ptr<DashboardContext> _dashboard;
    uint32_t _next_refresh_tick = 0;
};

}  // namespace setup_workers
