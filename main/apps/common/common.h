/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-30
 * 修改作用：固件版本改为读取 ESP-IDF 构建描述，避免界面硬编码版本与 Git/固件版本不一致。
 * 使用方式：在项目根 CMakeLists.txt 修改 PROJECT_VER，启动页与 About 会自动同步。
 */
#pragma once
#include <esp_app_desc.h>
#include <string_view>

namespace common {

inline std::string_view firmwareVersion()
{
    return esp_app_get_description()->version;
}

}
