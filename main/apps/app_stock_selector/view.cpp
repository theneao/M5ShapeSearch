/*
 * 创建时间：2026-08-28
 * 作用：实现 466×466 AMOLED 上的丝滑画板、结果卡片、缩略图与 K 线详情交互。
 * 修改时间：2026-08-29
 * 修改作用：形态页只保留绘制、结果和详情；网络状态只读显示，配置入口统一放到整机 Settings。
 * 使用方式：所有入口由 App 在 LVGL 锁内调用；网络回调只通过轮询结果进入本类。
 *
 * 修改时间：2026-08-29
 * 修改作用：匹配周期增加 5m/15m/30m/60m，与服务器缓存桶和数据设置保持一致。
 *
 * 修改时间：2026-08-29
 * 修改作用：K 线详情页的 +/- 使用比例缩放，并实时显示当前可见根数/总根数。
 *
 * 修改时间：2026-08-29
 * 修改作用：形态页切换跟随 Smooth/Eco 档位，统一使用非线性 ease-out 过渡。
 *
 * 修改时间：2026-08-30
 * 修改作用：参考 x-track 仅移动页面根节点，取消与位移动画叠加的全屏淡入，减少 RGB565 软件混合和无效区域。
 * 使用方式：绘制、结果和详情页自动使用 240ms/100ms 位移动画。
 *
 * 修改时间：2026-08-30
 * 修改作用：移除 466×466 页面根节点逐帧位移，内部页面改为一次提交，避免 QSPI 每帧传输约 434KB。
 * 使用方式：绘制、结果、详情与返回均立即切页；局部扫描和提示动画不受影响。
 *
 * 修改时间：2026-08-30
 * 修改作用：服务端中文或其他未内置字形的动态错误统一转换为英文提示，避免 AMOLED 显示方框。
 * 使用方式：showError() 自动检查 UTF-8 文本，静态英文提示不受影响。
 *
 * 修改时间：2026-08-30
 * 修改作用：Top10 列表启用纵向惯性滚动并关闭边缘橡皮筋，降低圆屏边界大面积反复重绘。
 * 使用方式：结果页上下拖动后可自然减速，点击卡片仍进入 K 线详情。
 */
#include "view.h"
// 修改时间：2026-08-29
// 修改作用：形态搜索仅显示 ALL、STOCK、CRYPTO，隐藏并停用期货入口。
// 使用方式：绘图页点击三个品类按钮之一后提交服务器匹配。
#include <hal/utils/settings/settings.h>

#include <algorithm>
#include <cmath>
#include <cstdio>

namespace stock_selector {
namespace {

constexpr int kScreenSize = 466;
constexpr uint32_t kBackground = 0x070B14;
constexpr uint32_t kPanel = 0x121A2A;
constexpr uint32_t kText = 0xF5F7FB;
constexpr uint32_t kMuted = 0x8D9AB0;
constexpr uint32_t kAccent = 0xFF8A34;
constexpr uint32_t kBlue = 0x3C82F6;
constexpr uint32_t kGreen = 0x31D0AA;
constexpr uint32_t kRed = 0xFF667A;

const char* kCategories[] = {"all", "stock", "crypto"};
const char* kCategoryLabels[] = {"ALL", "STOCK", "CRYPTO"};
constexpr int kCategoryCount = sizeof(kCategories) / sizeof(kCategories[0]);
const char* kTimeframes[] = {"1d", "1w", "4h", "60m", "30m", "15m", "5m"};
const char* kTimeframeLabels[] = {"1D", "1W", "4H", "60M", "30M", "15M", "5M"};
constexpr int kTimeframeCount = sizeof(kTimeframes) / sizeof(kTimeframes[0]);

void baseObject(lv_obj_t* object, uint32_t color, int radius = 0)
{
    lv_obj_set_style_bg_color(object, lv_color_hex(color), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(object, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(object, 0, LV_PART_MAIN);
    lv_obj_set_style_radius(object, radius, LV_PART_MAIN);
    lv_obj_set_style_pad_all(object, 0, LV_PART_MAIN);
}

}  // namespace

StockSelectorView::~StockSelectorView()
{
    destroyPage();
    if (_root != nullptr && lv_obj_is_valid(_root)) {
        lv_obj_delete(_root);
    }
    _root = nullptr;
}

void StockSelectorView::init(lv_obj_t* parent)
{
    _root = lv_obj_create(parent);
    lv_obj_set_size(_root, kScreenSize, kScreenSize);
    lv_obj_align(_root, LV_ALIGN_CENTER, 0, 0);
    baseObject(_root, kBackground);
    lv_obj_remove_flag(_root, LV_OBJ_FLAG_SCROLLABLE);
    _current_page = Page::Draw;
    createDrawPage(false);
}

lv_obj_t* StockSelectorView::createLabel(
    lv_obj_t* parent,
    const char* text,
    const lv_font_t* font,
    uint32_t color,
    lv_align_t align,
    int x,
    int y
)
{
    lv_obj_t* label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, font, LV_PART_MAIN);
    lv_obj_set_style_text_color(label, lv_color_hex(color), LV_PART_MAIN);
    lv_obj_align(label, align, x, y);
    return label;
}

lv_obj_t* StockSelectorView::createButton(
    lv_obj_t* parent,
    const char* text,
    int x,
    int y,
    int width,
    int height,
    uint32_t color,
    Action action,
    int value
)
{
    lv_obj_t* button = lv_button_create(parent);
    lv_obj_set_pos(button, x, y);
    lv_obj_set_size(button, width, height);
    baseObject(button, color, std::min(height / 2, 18));
    constexpr lv_style_selector_t pressed_selector =
        static_cast<lv_style_selector_t>(LV_PART_MAIN) |
        static_cast<lv_style_selector_t>(LV_STATE_PRESSED);
    lv_obj_set_style_bg_color(button, lv_color_hex(0xFFFFFF), pressed_selector);
    lv_obj_set_style_bg_opa(button, LV_OPA_30, pressed_selector);
    lv_obj_t* label = lv_label_create(button);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_14, LV_PART_MAIN);
    lv_obj_set_style_text_color(label, lv_color_hex(kText), LV_PART_MAIN);
    lv_obj_center(label);

    auto binding = std::make_unique<ButtonBinding>();
    binding->view = this;
    binding->action = action;
    binding->value = value;
    lv_obj_add_event_cb(button, &StockSelectorView::buttonEvent, LV_EVENT_CLICKED, binding.get());
    _bindings.push_back(std::move(binding));
    return button;
}

void StockSelectorView::buttonEvent(lv_event_t* event)
{
    auto* binding = static_cast<ButtonBinding*>(lv_event_get_user_data(event));
    if (binding != nullptr && binding->view != nullptr) {
        binding->view->handleAction(binding->action, binding->value);
    }
}

void StockSelectorView::destroyPage()
{
    _draw_canvas.reset();
    _previews.clear();
    _kline_chart.reset();
    _bindings.clear();
    _category_buttons.clear();
    _strategy_buttons.clear();
    _strategy_check_labels.clear();
    _network_label = nullptr;
    _wifi_icon = nullptr;
    _hint_label = nullptr;
    _busy_overlay = nullptr;
    _busy_label = nullptr;
    _scan_line = nullptr;
    _toast = nullptr;
    _detail_state_label = nullptr;
    _zoom_label = nullptr;
    _strategy_summary_label = nullptr;
    _strategy_mode_button = nullptr;
    _timeframe_button = nullptr;
    if (_page != nullptr && lv_obj_is_valid(_page)) {
        lv_obj_delete(_page);
    }
    _page = nullptr;
}

void StockSelectorView::animatePageIn(int fromX)
{
    (void)fromX;
    if (_page == nullptr) {
        return;
    }
    lv_obj_set_x(_page, 0);
}

void StockSelectorView::createDrawPage(bool animate)
{
    destroyPage();
    _current_page = Page::Draw;
    _page = lv_obj_create(_root);
    lv_obj_set_size(_page, kScreenSize, kScreenSize);
    baseObject(_page, kBackground);
    lv_obj_remove_flag(_page, LV_OBJ_FLAG_SCROLLABLE);

    createLabel(_page, "SHAPE SEARCH", &lv_font_montserrat_22, kText, LV_ALIGN_TOP_MID, 0, 18);
    lv_obj_t* network = lv_obj_create(_page);
    lv_obj_set_pos(network, 104, 50);
    lv_obj_set_size(network, 194, 34);
    baseObject(network, 0x19243A, 17);
    lv_obj_remove_flag(network, LV_OBJ_FLAG_SCROLLABLE);
    _network_label = createLabel(network, "CONNECTING", &lv_font_montserrat_10, kText,
                                 LV_ALIGN_CENTER, 11, 0);
    lv_obj_set_style_text_font(_network_label, &lv_font_montserrat_10, LV_PART_MAIN);
    _wifi_icon = createLabel(network, LV_SYMBOL_WIFI, &lv_font_montserrat_14, kMuted,
                             LV_ALIGN_LEFT_MID, 14, 0);

    int chip_x = 92;
    const int widths[] = {66, 78, 88};
    for (int index = 0; index < kCategoryCount; ++index) {
        lv_obj_t* chip = createButton(
            _page, kCategoryLabels[index], chip_x, 91, widths[index], 34, kPanel, ActionCategory, index
        );
        _category_buttons.push_back(chip);
        chip_x += widths[index] + 6;
    }
    _timeframe_button = createButton(_page, "1D", 306, 50, 60, 34, 0x1A2942, ActionTimeframe);
    styleChipSelection();

    _draw_canvas = std::make_unique<DrawCanvas>(_page);
    lv_obj_align(_draw_canvas->object(), LV_ALIGN_TOP_MID, 0, 135);
    _hint_label = createLabel(
        _draw_canvas->object(), "DRAW LEFT TO RIGHT", &lv_font_montserrat_16, 0x5E6A80,
        LV_ALIGN_CENTER, 0, 0
    );
    lv_obj_remove_flag(_hint_label, LV_OBJ_FLAG_CLICKABLE);

    createButton(_page, "FILTER", 28, 50, 68, 34, 0x1A2942, ActionOpenStrategies);
    createButton(_page, "CLEAR", 58, 347, 84, 62, 0x263147, ActionClear);
    createButton(_page, "SAVE", 150, 347, 82, 62, kBlue, ActionSaveStrategy);
    createButton(_page, "MATCH", 240, 347, 166, 62, kAccent, ActionMatch);

    createLabel(_page, "KEY A: EXIT / CANCEL    KEY B: MATCH", &lv_font_montserrat_10, kMuted,
                LV_ALIGN_BOTTOM_MID, 0, -36);
    if (animate) {
        animatePageIn(-56);
    }
}

void StockSelectorView::createStrategiesPage(bool animate)
{
    destroyPage();
    _current_page = Page::Strategies;
    _page = lv_obj_create(_root);
    lv_obj_set_size(_page, kScreenSize, kScreenSize);
    baseObject(_page, kBackground);
    lv_obj_remove_flag(_page, LV_OBJ_FLAG_SCROLLABLE);

    createLabel(_page, "STRATEGY FILTER", &lv_font_montserrat_20, kText, LV_ALIGN_TOP_MID, 0, 22);
    createLabel(_page, "TAP TO SELECT  |  KEY A: BACK  KEY B: RUN", &lv_font_montserrat_10,
                kMuted, LV_ALIGN_TOP_MID, 0, 49);

    lv_obj_t* list = lv_obj_create(_page);
    lv_obj_set_pos(list, 66, 72);
    lv_obj_set_size(list, 334, 278);
    baseObject(list, kBackground);
    lv_obj_set_scroll_dir(list, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(list, LV_SCROLLBAR_MODE_ACTIVE);
    lv_obj_add_flag(list, LV_OBJ_FLAG_SCROLL_MOMENTUM);
    lv_obj_remove_flag(list, LV_OBJ_FLAG_SCROLL_ELASTIC);
    lv_obj_set_style_pad_bottom(list, 8, LV_PART_MAIN);

    for (std::size_t index = 0; index < _strategies.size(); ++index) {
        const auto& strategy = _strategies[index];
        lv_obj_t* row = createButton(
            list, "", 12, static_cast<int>(index) * 54, 286, 46,
            strategy.selected ? 0x73451F : kPanel,
            ActionToggleStrategy, static_cast<int>(index)
        );
        _strategy_buttons.push_back(row);
        lv_obj_t* name = createLabel(
            row, strategy.shortName.c_str(), &lv_font_montserrat_14, kText,
            LV_ALIGN_LEFT_MID, 18, 0
        );
        lv_obj_set_width(name, 190);
        lv_label_set_long_mode(name, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
        _strategy_check_labels.push_back(createLabel(
            row, strategy.selected ? LV_SYMBOL_OK : "+", &lv_font_montserrat_16,
            strategy.selected ? kGreen : kMuted, LV_ALIGN_RIGHT_MID, -18, 0
        ));
    }
    if (_strategies.empty()) {
        createLabel(list, "LOADING STRATEGIES", &lv_font_montserrat_16, kMuted,
                    LV_ALIGN_CENTER, 0, 0);
    }

    _strategy_mode_button = createButton(
        _page, _strategy_intersection ? "MODE: AND" : "MODE: OR",
        88, 365, 132, 54, 0x263147, ActionCombine
    );
    createButton(_page, "RUN FILTER", 230, 365, 150, 54, kAccent, ActionRunStrategies);
    char market[64] = {};
    std::snprintf(market, sizeof(market), "%s  %s  |  %u SELECTED",
                  _category.c_str(), _timeframe.c_str(),
                  static_cast<unsigned>(std::count_if(
                      _strategies.begin(), _strategies.end(),
                      [](const StrategyDefinition& item) { return item.selected; }
                  )));
    _strategy_summary_label = createLabel(
        _page, market, &lv_font_montserrat_10, kMuted, LV_ALIGN_BOTTOM_MID, 0, -22
    );
    if (animate) {
        animatePageIn(64);
    }
}

void StockSelectorView::createResultsPage(bool animate)
{
    destroyPage();
    _current_page = Page::Results;
    _page = lv_obj_create(_root);
    lv_obj_set_size(_page, kScreenSize, kScreenSize);
    baseObject(_page, kBackground);
    lv_obj_remove_flag(_page, LV_OBJ_FLAG_SCROLLABLE);

    createLabel(_page, _strategy_results_mode ? "STRATEGY HITS" : "TOP MATCHES",
                &lv_font_montserrat_20, kText, LV_ALIGN_TOP_MID, 0, 30);
    char timing[40] = {};
    std::snprintf(timing, sizeof(timing), "%u hits  |  %d ms", static_cast<unsigned>(_results.size()), _query_ms);
    createLabel(_page, timing, &lv_font_montserrat_14, kMuted, LV_ALIGN_TOP_MID, 0, 60);
    createLabel(_page, "KEY A / SWIPE LEFT: BACK    KEY B: TOP1", &lv_font_montserrat_10,
                kMuted, LV_ALIGN_TOP_MID, 0, 78);

    lv_obj_t* list = lv_obj_create(_page);
    lv_obj_set_pos(list, 72, 98);
    lv_obj_set_size(list, 322, 326);
    baseObject(list, kBackground);
    lv_obj_set_style_pad_bottom(list, 12, LV_PART_MAIN);
    lv_obj_set_scroll_dir(list, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(list, LV_SCROLLBAR_MODE_ACTIVE);
    lv_obj_add_flag(list, LV_OBJ_FLAG_SCROLL_MOMENTUM);
    lv_obj_remove_flag(list, LV_OBJ_FLAG_SCROLL_ELASTIC);
    lv_obj_set_style_anim_duration(list, 180, LV_PART_MAIN);

    for (std::size_t index = 0; index < _results.size(); ++index) {
        const MatchResult& result = _results[index];
        lv_obj_t* card = createButton(
            list, "", 25, static_cast<int>(index) * 116, 268, 104, kPanel, ActionResult,
            static_cast<int>(index)
        );
        lv_obj_set_style_radius(card, 18, LV_PART_MAIN);
        if (!_strategy_results_mode) {
            auto preview = std::make_unique<LinePreview>(card, _query_points, result.preview);
            lv_obj_set_pos(preview->object(), 8, 8);
            lv_obj_set_size(preview->object(), 104, 88);
            _previews.push_back(std::move(preview));
        } else {
            createLabel(card, "RULE", &lv_font_montserrat_18, kBlue,
                        LV_ALIGN_LEFT_MID, 25, 0);
        }

        lv_obj_t* symbol = createLabel(card, result.symbol.c_str(), &lv_font_montserrat_14, kText,
                                       LV_ALIGN_TOP_LEFT, 121, 10);
        lv_obj_set_width(symbol, 140);
        lv_label_set_long_mode(symbol, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
        char score[24] = {};
        std::snprintf(score, sizeof(score), "#%u   %.1f%%", static_cast<unsigned>(index + 1),
                      result.matchScore * 100.0f);
        createLabel(card, score, &lv_font_montserrat_18, kAccent, LV_ALIGN_TOP_LEFT, 121, 37);
        char change[48] = {};
        std::snprintf(change, sizeof(change), _strategy_results_mode ? "%s %+.1f%% %d RULES" : "%s %+.1f%% %dK",
                      result.category.c_str(), result.changePct, result.rawBars);
        createLabel(card, change, &lv_font_montserrat_14,
                    result.changePct >= 0 ? kGreen : kRed, LV_ALIGN_TOP_LEFT, 121, 72);
    }
    if (_results.empty()) {
        createLabel(list, "NO MATCHES", &lv_font_montserrat_20, kMuted, LV_ALIGN_CENTER, 0, -10);
    }
    if (animate) {
        animatePageIn(64);
    }
}

void StockSelectorView::createDetailPage(bool animate)
{
    destroyPage();
    _current_page = Page::Detail;
    _page = lv_obj_create(_root);
    lv_obj_set_size(_page, kScreenSize, kScreenSize);
    baseObject(_page, kBackground);
    lv_obj_remove_flag(_page, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t* symbol = createLabel(_page, _selected.symbol.c_str(), &lv_font_montserrat_18, kText,
                                   LV_ALIGN_TOP_MID, 0, 28);
    lv_obj_set_width(symbol, 230);
    lv_obj_align(symbol, LV_ALIGN_TOP_MID, 0, 28);
    lv_label_set_long_mode(symbol, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    char score[20] = {};
    std::snprintf(score, sizeof(score), "%.1f%%", _selected.matchScore * 100.0f);
    createLabel(_page, score, &lv_font_montserrat_18, kAccent, LV_ALIGN_TOP_MID, 0, 58);
    char summary[80] = {};
    std::snprintf(summary, sizeof(summary), "%s  %s  %+.2f%%  %.4g", _selected.category.c_str(),
                  _timeframe.c_str(), _selected.changePct, _selected.currentPrice);
    createLabel(_page, summary, &lv_font_montserrat_14,
                _selected.changePct >= 0 ? kGreen : kRed, LV_ALIGN_TOP_MID, 0, 82);

    _kline_chart = std::make_unique<KlineChart>(_page);
    lv_obj_set_pos(_kline_chart->object(), 36, 110);
    lv_obj_set_size(_kline_chart->object(), 394, 238);
    _detail_state_label = createLabel(_page, "LOADING KLINE FROM SERVER", &lv_font_montserrat_14, kMuted,
                                      LV_ALIGN_TOP_MID, 0, 120);

    if (_strategy_results_mode) {
        char explain[64] = {};
        std::snprintf(explain, sizeof(explain), "MATCHED %d SELECTED RULES", _selected.rawBars);
        createLabel(_page, explain, &lv_font_montserrat_14, kMuted, LV_ALIGN_TOP_MID, 0, 361);
    } else {
        char explain_primary[64] = {};
        std::snprintf(explain_primary, sizeof(explain_primary), "NCC %.2f   dNCC %.2f",
                      _selected.details.priceNcc, _selected.details.derivativeNcc);
        createLabel(_page, explain_primary, &lv_font_montserrat_14, kMuted, LV_ALIGN_TOP_MID, 0, 351);
        char explain_secondary[64] = {};
        std::snprintf(explain_secondary, sizeof(explain_secondary), "TURN %.2f   DTW %.2f",
                      _selected.details.turningScore, _selected.details.shapeDtwScore);
        createLabel(_page, explain_secondary, &lv_font_montserrat_14, kMuted, LV_ALIGN_TOP_MID, 0, 374);
    }
    createButton(_page, "-", 130, 397, 64, 44, 0x243149, ActionZoomOut);
    _zoom_label = createLabel(_page, "ZOOM --/--", &lv_font_montserrat_14, kMuted,
                              LV_ALIGN_BOTTOM_MID, 0, -27);
    createButton(_page, "+", 272, 397, 64, 44, 0x243149, ActionZoomIn);
    if (animate) {
        animatePageIn(64);
    }
}

void StockSelectorView::styleChipSelection()
{
    for (std::size_t index = 0; index < _category_buttons.size(); ++index) {
        lv_obj_set_style_bg_color(
            _category_buttons[index],
            lv_color_hex(_category == kCategories[index] ? kAccent : kPanel),
            LV_PART_MAIN
        );
    }
    for (int index = 0; index < kTimeframeCount; ++index) {
        if (_timeframe == kTimeframes[index] && _timeframe_button != nullptr) {
            lv_label_set_text(lv_obj_get_child(_timeframe_button, 0), kTimeframeLabels[index]);
            break;
        }
    }
}

void StockSelectorView::updateStrategySelectionUi(int changedIndex)
{
    if (changedIndex >= 0 && changedIndex < static_cast<int>(_strategies.size()) &&
        changedIndex < static_cast<int>(_strategy_buttons.size()) &&
        changedIndex < static_cast<int>(_strategy_check_labels.size())) {
        const bool selected = _strategies[changedIndex].selected;
        lv_obj_set_style_bg_color(
            _strategy_buttons[changedIndex],
            lv_color_hex(selected ? 0x73451F : kPanel),
            LV_PART_MAIN
        );
        lv_label_set_text(_strategy_check_labels[changedIndex], selected ? LV_SYMBOL_OK : "+");
        lv_obj_set_style_text_color(
            _strategy_check_labels[changedIndex],
            lv_color_hex(selected ? kGreen : kMuted),
            LV_PART_MAIN
        );
    }

    if (_strategy_mode_button != nullptr) {
        lv_obj_t* label = lv_obj_get_child(_strategy_mode_button, 0);
        if (label != nullptr) {
            lv_label_set_text(label, _strategy_intersection ? "MODE: AND" : "MODE: OR");
        }
    }
    if (_strategy_summary_label != nullptr) {
        char market[64] = {};
        std::snprintf(market, sizeof(market), "%s  %s  |  %u SELECTED",
                      _category.c_str(), _timeframe.c_str(),
                      static_cast<unsigned>(std::count_if(
                          _strategies.begin(), _strategies.end(),
                          [](const StrategyDefinition& item) { return item.selected; }
                      )));
        lv_label_set_text(_strategy_summary_label, market);
    }
}

void StockSelectorView::handleAction(Action action, int value)
{
    switch (action) {
        case ActionBack:
            goBack();
            break;
        case ActionClear:
            if (_draw_canvas) {
                _draw_canvas->clear();
            }
            break;
        case ActionMatch:
            if (_draw_canvas && !_draw_canvas->empty() && !_busy) {
                _query_points = _draw_canvas->normalizedPoints(128);
                if (onMatchRequested) {
                    onMatchRequested(_query_points, _category, _timeframe);
                }
            } else if (!_busy) {
                showError("Draw a curve first");
            }
            break;
        case ActionSaveStrategy:
            if (_draw_canvas && !_draw_canvas->empty() && !_busy) {
                _query_points = _draw_canvas->normalizedPoints(128);
                if (onSaveStrategyRequested) {
                    onSaveStrategyRequested(_query_points);
                }
            } else if (!_busy) {
                showError("Draw a curve first");
            }
            break;
        case ActionOpenStrategies:
            if (!_busy) {
                createStrategiesPage(true);
                setBusy(true, "LOADING STRATEGIES");
                if (onStrategyCatalogRequested) {
                    onStrategyCatalogRequested();
                }
            }
            break;
        case ActionToggleStrategy:
            if (value >= 0 && value < static_cast<int>(_strategies.size())) {
                _strategies[value].selected = !_strategies[value].selected;
                updateStrategySelectionUi(value);
            }
            break;
        case ActionCombine:
            _strategy_intersection = !_strategy_intersection;
            updateStrategySelectionUi();
            break;
        case ActionRunStrategies: {
            if (_busy) {
                break;
            }
            std::vector<std::string> ids;
            for (const auto& strategy : _strategies) {
                if (strategy.selected) {
                    ids.push_back(strategy.id);
                }
            }
            if (ids.empty()) {
                showError("Select at least one strategy");
            } else if (onStrategyScreenRequested) {
                onStrategyScreenRequested(ids, _strategy_intersection, _category, _timeframe);
            }
            break;
        }
        case ActionCategory:
            if (value >= 0 && value < kCategoryCount) {
                _category = kCategories[value];
                styleChipSelection();
            }
            break;
        case ActionTimeframe: {
            int next = 0;
            for (int index = 0; index < kTimeframeCount; ++index) {
                if (_timeframe == kTimeframes[index]) {
                    next = (index + 1) % kTimeframeCount;
                    break;
                }
            }
            _timeframe = kTimeframes[next];
            styleChipSelection();
            break;
        }
        case ActionResult:
            if (value >= 0 && value < static_cast<int>(_results.size())) {
                _selected = _results[value];
                createDetailPage(true);
                setBusy(true, "LOADING DETAIL");
                if (onDetailRequested) {
                    onDetailRequested(_selected, _timeframe);
                }
            }
            break;
        case ActionZoomOut:
            if (_kline_chart) {
                _kline_chart->zoomOut();
                updateZoomLabel();
            }
            break;
        case ActionZoomIn:
            if (_kline_chart) {
                _kline_chart->zoomIn();
                updateZoomLabel();
            }
            break;
    }
}

void StockSelectorView::showResults(const std::vector<MatchResult>& results, int queryMs)
{
    _strategy_results_mode = false;
    _results = results;
    _query_ms = queryMs;
    _busy = false;
    createResultsPage(true);
}

void StockSelectorView::setStrategies(const std::vector<StrategyDefinition>& strategies)
{
    _strategies = strategies;
    _busy = false;
    createStrategiesPage(false);
}

void StockSelectorView::showStrategyResults(const std::vector<MatchResult>& results, int queryMs)
{
    _strategy_results_mode = true;
    _results = results;
    _query_ms = queryMs;
    _busy = false;
    createResultsPage(true);
}

void StockSelectorView::showStrategySaved()
{
    setBusy(false);
    showToast("SKETCH STRATEGY SAVED", 0x17604E);
}

void StockSelectorView::setDetail(const KlineDetail& detail)
{
    if (_current_page != Page::Detail || !_kline_chart) {
        return;
    }
    _kline_chart->setData(detail);
    updateZoomLabel();
    _busy = false;
    if (_detail_state_label != nullptr) {
        char text[52] = {};
        std::snprintf(text, sizeof(text), "%u BARS  |  %s + MA",
                      static_cast<unsigned>(detail.bars.size()),
                      detail.ohlcExact ? "REAL OHLC" : "CLOSE SERIES");
        lv_label_set_text(_detail_state_label, text);
        lv_obj_set_style_text_color(_detail_state_label, lv_color_hex(detail.ohlcExact ? kGreen : kMuted),
                                    LV_PART_MAIN);
    }
    setBusy(false);
}

void StockSelectorView::updateZoomLabel()
{
    if (_zoom_label == nullptr || _kline_chart == nullptr) {
        return;
    }
    char text[32] = {};
    std::snprintf(text, sizeof(text), "ZOOM %d/%d BARS",
                  _kline_chart->visibleCount(), _kline_chart->totalCount());
    lv_label_set_text(_zoom_label, text);
}

void StockSelectorView::setBusy(bool busy, const char* text)
{
    _busy = busy;
    if (busy) {
        _busy_text = text != nullptr ? text : "WAITING FOR SERVER";
        _busy_started = lv_tick_get();
        _last_busy_second = UINT32_MAX;
    }
    if (_current_page == Page::Draw) {
        if (busy && _draw_canvas && _scan_line == nullptr) {
            _scan_line = lv_obj_create(_draw_canvas->object());
            lv_obj_set_size(_scan_line, 4, 172);
            baseObject(_scan_line, kAccent, 2);
            lv_obj_remove_flag(_scan_line, LV_OBJ_FLAG_CLICKABLE);
            lv_obj_remove_flag(_scan_line, LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_style_shadow_color(_scan_line, lv_color_hex(kAccent), LV_PART_MAIN);
            lv_obj_set_style_shadow_width(_scan_line, 14, LV_PART_MAIN);
            lv_obj_set_style_shadow_opa(_scan_line, LV_OPA_60, LV_PART_MAIN);
        } else if (!busy && _scan_line != nullptr) {
            lv_obj_delete(_scan_line);
            _scan_line = nullptr;
        }
    }
    if (busy && _busy_overlay == nullptr && _page != nullptr) {
        _busy_overlay = lv_obj_create(_page);
        lv_obj_set_size(_busy_overlay, 286, 76);
        lv_obj_align(_busy_overlay, LV_ALIGN_CENTER, 0, 4);
        baseObject(_busy_overlay, 0x1A2335, 29);
        lv_obj_set_style_bg_opa(_busy_overlay, LV_OPA_90, LV_PART_MAIN);
        lv_obj_t* spinner = lv_spinner_create(_busy_overlay);
        lv_obj_set_size(spinner, 30, 30);
        lv_obj_align(spinner, LV_ALIGN_LEFT_MID, 14, 0);
        lv_obj_set_style_arc_color(spinner, lv_color_hex(kAccent), LV_PART_INDICATOR);
        _busy_label = createLabel(_busy_overlay, text, &lv_font_montserrat_14, kText,
                                  LV_ALIGN_LEFT_MID, 56, 0);
        lv_obj_set_width(_busy_label, 210);
        lv_label_set_long_mode(_busy_label, LV_LABEL_LONG_MODE_WRAP);
    } else if (!busy && _busy_overlay != nullptr) {
        lv_obj_delete(_busy_overlay);
        _busy_overlay = nullptr;
        _busy_label = nullptr;
    }
}

void StockSelectorView::showError(const std::string& message)
{
    setBusy(false);
    const std::string display = asciiOrFallback(
        message, "SERVER DATA ERROR; CHECK NETWORK OR DATA STATUS"
    );
    showToast(display, 0x6C2631);
}

void StockSelectorView::showToast(const std::string& message, uint32_t color)
{
    if (_toast != nullptr) {
        lv_obj_delete(_toast);
    }
    _toast = lv_obj_create(_page);
    lv_obj_set_size(_toast, 236, 54);
    lv_obj_align(_toast, LV_ALIGN_BOTTOM_MID, 0, -32);
    baseObject(_toast, color, 24);
    lv_obj_set_style_bg_opa(_toast, LV_OPA_90, LV_PART_MAIN);
    lv_obj_t* label = createLabel(_toast, message.c_str(), &lv_font_montserrat_14, kText,
                                  LV_ALIGN_CENTER, 0, 0);
    lv_obj_set_width(label, 206);
    lv_label_set_long_mode(label, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, LV_PART_MAIN);
    _toast_until = lv_tick_get() + 3600;
    lv_obj_fade_in(_toast, 140, 0);
}

bool StockSelectorView::goBack()
{
    if (_current_page == Page::Detail) {
        createResultsPage(true);
        return true;
    }
    if (_current_page == Page::Results) {
        if (_strategy_results_mode) {
            createStrategiesPage(true);
        } else {
            createDrawPage(true);
        }
        return true;
    }
    if (_current_page == Page::Strategies) {
        createDrawPage(true);
        return true;
    }
    return false;
}

void StockSelectorView::triggerPrimary()
{
    if (_busy) {
        return;
    }
    if (_current_page == Page::Draw) {
        handleAction(ActionMatch, 0);
    } else if (_current_page == Page::Strategies) {
        handleAction(ActionRunStrategies, 0);
    } else if (_current_page == Page::Results) {
        if (!_results.empty()) {
            handleAction(ActionResult, 0);
        }
    } else if (_current_page == Page::Detail && _kline_chart) {
        _kline_chart->zoomIn();
        updateZoomLabel();
    }
}

void StockSelectorView::updateBusyAnimation(uint32_t now)
{
    if (_busy && _scan_line != nullptr) {
        const int x = 8 + static_cast<int>((now % 1100) * 386 / 1100);
        lv_obj_set_pos(_scan_line, x, 13);
    }
    if (_busy && _busy_label != nullptr) {
        const uint32_t seconds = (now - _busy_started) / 1000;
        if (seconds != _last_busy_second) {
            char text[96] = {};
            std::snprintf(text, sizeof(text), "%s  %us\nKEY A / SWIPE LEFT TO CANCEL",
                          _busy_text.c_str(), static_cast<unsigned>(seconds));
            lv_label_set_text(_busy_label, text);
            _last_busy_second = seconds;
        }
    }
}

void StockSelectorView::update(uint32_t now, const std::string& networkStatus, int rssi)
{
    if (_network_label != nullptr && now - _last_network_update >= 500) {
        char status[64] = {};
        if (rssi > -120) {
            std::snprintf(status, sizeof(status), "%s  %d dBm", networkStatus.c_str(), rssi);
        } else {
            std::snprintf(status, sizeof(status), "%s", networkStatus.c_str());
        }
        lv_label_set_text(_network_label, status);
        if (_wifi_icon != nullptr) {
            const uint32_t color = rssi <= -120 ? kMuted : rssi <= -80 ? kRed : rssi <= -70 ? kAccent : kGreen;
            lv_obj_set_style_text_color(_wifi_icon, lv_color_hex(color), LV_PART_MAIN);
            lv_label_set_text(_wifi_icon, rssi <= -120 ? LV_SYMBOL_CLOSE : LV_SYMBOL_WIFI);
        }
        _last_network_update = now;
    }
    if (_hint_label != nullptr && _draw_canvas != nullptr) {
        if (_draw_canvas->empty()) {
            lv_obj_remove_flag(_hint_label, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(_hint_label, LV_OBJ_FLAG_HIDDEN);
        }
    }
    updateBusyAnimation(now);
    if (_toast != nullptr && static_cast<int32_t>(now - _toast_until) >= 0) {
        lv_obj_delete(_toast);
        _toast = nullptr;
    }
}

std::string StockSelectorView::asciiOrFallback(const std::string& text, const std::string& fallback)
{
    return std::all_of(text.begin(), text.end(), [](unsigned char value) { return value < 0x80; }) ? text : fallback;
}

}  // namespace stock_selector
