/*
 * 创建时间：2026-08-28
 * 作用：定义硬件端形态检索、缩略图和 K 线详情共享的轻量数据结构。
 * 使用方式：App、网络服务和 LVGL 组件共同包含本文件；不在端侧执行匹配算法。
 */
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace stock_selector {

struct NormalizedPoint {
    float x = 0.0f;
    float y = 0.0f;
};

struct MatchScoreDetails {
    float priceNcc       = 0.0f;
    float derivativeNcc  = 0.0f;
    float turningScore   = 0.0f;
    float shapeDtwScore  = 0.0f;
    float shapeDtwDist   = 0.0f;
};

struct MatchResult {
    std::string symbol;
    std::string name;
    std::string category;
    float matchScore  = 0.0f;
    float currentPrice = 0.0f;
    float changePct    = 0.0f;
    int64_t windowStartTs = 0;
    int64_t windowEndTs   = 0;
    int rawBars = 0;
    MatchScoreDetails details;
    std::vector<NormalizedPoint> preview;
};

struct KlineBar {
    int64_t timestamp = 0;
    float open   = 0.0f;
    float high   = 0.0f;
    float low    = 0.0f;
    float close  = 0.0f;
    float volume = 0.0f;
};

struct KlineDetail {
    std::string symbol;
    std::string name;
    std::string category;
    std::string timeframe = "1d";
    bool hasMore  = false;
    bool ohlcExact = false;
    std::vector<KlineBar> bars;
};

}  // namespace stock_selector
