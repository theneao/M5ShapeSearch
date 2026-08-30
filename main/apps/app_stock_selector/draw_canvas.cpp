/*
 * 创建时间：2026-08-28
 * 作用：实现轻量手绘画板；端侧只采样和归一化，不计算搜索特征。
 * 使用方式：由 StockSelectorView 管理生命周期，必须在持有 LVGL 锁时创建/销毁。
 */
#include "draw_canvas.h"

#include <algorithm>
#include <cmath>

namespace stock_selector {
namespace {

// 466×466 圆屏安全区：上下边缘越靠近圆周，可用横向宽度越小。
constexpr int kCanvasWidth = 406;
constexpr int kCanvasHeight = 198;
constexpr std::size_t kRawPointLimit = 320;

void drawLine(
    lv_layer_t* layer,
    int x1,
    int y1,
    int x2,
    int y2,
    lv_color_t color,
    int width,
    lv_opa_t opacity = LV_OPA_COVER
)
{
    lv_draw_line_dsc_t descriptor;
    lv_draw_line_dsc_init(&descriptor);
    descriptor.p1.x = x1;
    descriptor.p1.y = y1;
    descriptor.p2.x = x2;
    descriptor.p2.y = y2;
    descriptor.color = color;
    descriptor.width = width;
    descriptor.opa = opacity;
    descriptor.round_start = true;
    descriptor.round_end = true;
    lv_draw_line(layer, &descriptor);
}

}  // namespace

DrawCanvas::DrawCanvas(lv_obj_t* parent)
{
    _points.reserve(kRawPointLimit);
    _object = lv_obj_create(parent);
    lv_obj_set_size(_object, kCanvasWidth, kCanvasHeight);
    lv_obj_set_style_radius(_object, 22, LV_PART_MAIN);
    lv_obj_set_style_bg_color(_object, lv_color_hex(0x111827), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(_object, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_color(_object, lv_color_hex(0x263349), LV_PART_MAIN);
    lv_obj_set_style_border_width(_object, 1, LV_PART_MAIN);
    lv_obj_set_style_pad_all(_object, 0, LV_PART_MAIN);
    lv_obj_remove_flag(_object, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(_object, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(_object, LV_OBJ_FLAG_GESTURE_BUBBLE);
    lv_obj_add_event_cb(_object, &DrawCanvas::eventHandler, LV_EVENT_ALL, this);
}

DrawCanvas::~DrawCanvas()
{
    if (_object != nullptr && lv_obj_is_valid(_object)) {
        lv_obj_delete(_object);
    }
    _object = nullptr;
}

void DrawCanvas::clear()
{
    _points.clear();
    _drawing = false;
    if (_object != nullptr) {
        lv_obj_invalidate(_object);
    }
}

std::vector<NormalizedPoint> DrawCanvas::normalizedPoints(std::size_t maxPoints) const
{
    std::vector<NormalizedPoint> output;
    if (_points.size() < 3) {
        return output;
    }
    maxPoints = std::clamp<std::size_t>(maxPoints, 3, 256);
    const std::size_t count = std::min(maxPoints, _points.size());
    output.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
        const float source = static_cast<float>(index) * static_cast<float>(_points.size() - 1)
            / static_cast<float>(count - 1);
        const std::size_t left = static_cast<std::size_t>(source);
        const std::size_t right = std::min(left + 1, _points.size() - 1);
        const float fraction = source - static_cast<float>(left);
        const float px = _points[left].x + (_points[right].x - _points[left].x) * fraction;
        const float py = _points[left].y + (_points[right].y - _points[left].y) * fraction;
        output.push_back({
            std::clamp(px / static_cast<float>(kCanvasWidth - 1), 0.0f, 1.0f),
            std::clamp(1.0f - py / static_cast<float>(kCanvasHeight - 1), 0.0f, 1.0f),
        });
    }
    return output;
}

void DrawCanvas::eventHandler(lv_event_t* event)
{
    auto* self = static_cast<DrawCanvas*>(lv_event_get_user_data(event));
    if (self == nullptr) {
        return;
    }
    const lv_event_code_t code = lv_event_get_code(event);
    if (code == LV_EVENT_DRAW_MAIN) {
        self->draw(lv_event_get_layer(event));
    } else if (code == LV_EVENT_PRESSED || code == LV_EVENT_PRESSING || code == LV_EVENT_RELEASED ||
               code == LV_EVENT_PRESS_LOST) {
        self->handleTouch(code, lv_indev_active());
    }
}

void DrawCanvas::handleTouch(lv_event_code_t code, lv_indev_t* input)
{
    if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
        _drawing = false;
        return;
    }
    if (input == nullptr) {
        return;
    }
    if (code == LV_EVENT_PRESSED) {
        _drawing = true;
    }
    if (!_drawing || _points.size() >= kRawPointLimit) {
        return;
    }

    const uint32_t now = lv_tick_get();
    if (code == LV_EVENT_PRESSING && now - _last_sample_tick < 12) {
        return;
    }
    lv_point_t point;
    lv_indev_get_point(input, &point);
    lv_area_t area;
    lv_obj_get_coords(_object, &area);
    int x = std::clamp<int>(point.x - area.x1, 0, kCanvasWidth - 1);
    const int y = std::clamp<int>(point.y - area.y1, 0, kCanvasHeight - 1);
    if (!_points.empty()) {
        x = std::max<int>(x, _points.back().x);
        const int dx = x - _points.back().x;
        const int dy = y - _points.back().y;
        if (dx * dx + dy * dy < 5 && code != LV_EVENT_PRESSED) {
            return;
        }
    }
    _points.push_back({static_cast<int16_t>(x), static_cast<int16_t>(y)});
    _last_sample_tick = now;
    lv_obj_invalidate(_object);
}

void DrawCanvas::draw(lv_layer_t* layer)
{
    lv_area_t area;
    lv_obj_get_coords(_object, &area);
    for (int step = 1; step < 4; ++step) {
        const int x = area.x1 + step * kCanvasWidth / 4;
        const int y = area.y1 + step * kCanvasHeight / 4;
        drawLine(layer, x, area.y1 + 8, x, area.y2 - 8, lv_color_hex(0x25324A), 1, LV_OPA_50);
        drawLine(layer, area.x1 + 8, y, area.x2 - 8, y, lv_color_hex(0x25324A), 1, LV_OPA_50);
    }
    if (_points.size() < 2) {
        return;
    }
    for (std::size_t index = 1; index < _points.size(); ++index) {
        drawLine(
            layer,
            area.x1 + _points[index - 1].x,
            area.y1 + _points[index - 1].y,
            area.x1 + _points[index].x,
            area.y1 + _points[index].y,
            lv_color_hex(0xFF9D3D),
            5
        );
    }
}

}  // namespace stock_selector
