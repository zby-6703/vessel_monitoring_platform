"""Validated monitoring policy, independent of camera I/O and runtime state."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    enabled: bool = False
    recording_mode: Literal["optical_cycle", "passage"] = "optical_cycle"
    departure_confirm_seconds: float = Field(default=5, ge=.3, le=30)
    departure_min_observations: int = Field(default=3, ge=2, le=30)
    wait_full_body: bool = True
    body_entry_margin: float = Field(default=.01, ge=.01, le=.2)
    body_entry_confirm_seconds: float = Field(default=.4, ge=.2, le=10)
    body_entry_max_wait_seconds: float = Field(default=3, ge=1, le=30)
    zoom_min_hold_seconds: float = Field(default=12, ge=0, le=120)
    auto_horizontal_follow: bool = False
    detail_capture_priority: bool = True
    pan_dead_zone: float = Field(default=.15, ge=.05, le=.35)
    pan_step_limit: float = Field(default=.3, ge=.05, le=.6)
    pan_cooldown_seconds: float = Field(default=2, ge=.5, le=15)
    pan_max_travel_ratio: float = Field(default=.15, ge=.01, le=.4)
    zoom_settle_timeout_seconds: float = Field(default=15, ge=3, le=60)
    overview_settle_seconds: float = Field(default=.5, ge=0, le=5)
    adaptive_zoom_enabled: bool = True
    target_height_ratio: float = Field(default=1., ge=.15, le=1.)
    min_optical_zoom: float = Field(default=3, ge=1.1, le=40)
    max_optical_zoom: float = Field(default=8, ge=1.1, le=40)
    suppress_stationary: bool = True
    stationary_seconds: float = Field(default=15, ge=3, le=600)
    stationary_motion_threshold: float = Field(default=.015, ge=.002, le=.1)
    motion_confirm_seconds: float = Field(default=1, ge=.2, le=10)
    target_switch_margin: float = Field(default=.2, ge=0, le=1)
    zoom_adjust_cooldown_seconds: float = Field(default=3, ge=1, le=30)
    zoom_adjust_hysteresis: float = Field(default=.15, ge=.05, le=.5)
    target_acquire_timeout_seconds: float = Field(default=45, ge=3, le=180)
    pre_roll_seconds: float = Field(default=3, ge=0, le=20)
    post_roll_seconds: float = Field(default=5, ge=1, le=60)
    min_hits: int = Field(default=1, ge=1, le=20)
    confidence_threshold: float = Field(default=.45, ge=.1, le=1)
    max_clip_seconds: float = Field(default=300, ge=5, le=3600)
    max_storage_gb: float = Field(default=10, ge=.1, le=10000)
    retention_days: int = Field(default=30, ge=1, le=3650)
    focus_mode: Literal["auto", "manual"] = "auto"
    zoom_factor: float = Field(default=4, ge=1, le=40)
    center_x: float = Field(default=.5, ge=0, le=1)
    center_y: float = Field(default=.5, ge=0, le=1)
    padding: float = Field(default=.2, ge=0, le=1)
    # Realtime inference admission is independent of the recording switch.
    enable_ship_name: bool = True
    enable_draft: bool = True
    auto_region_focus: bool = False
    target_filter_enabled: bool = True
    min_target_height_ratio: float = Field(default=.06, ge=0, le=.8)
    min_target_width_ratio: float = Field(default=.12, ge=0, le=1)
    target_confirm_hits: int = Field(default=3, ge=1, le=30)
    target_confirm_seconds: float = Field(default=.4, ge=0, le=10)
    target_roi_left: float = Field(default=0, ge=0, le=1)
    target_roi_top: float = Field(default=0, ge=0, le=1)
    target_roi_right: float = Field(default=1, ge=0, le=1)
    target_roi_bottom: float = Field(default=1, ge=0, le=1)
    detail_interval_seconds: float = Field(default=.8, ge=0, le=30)
    max_detail_targets: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def validate_roll(self):
        if self.target_roi_left >= self.target_roi_right or self.target_roi_top >= self.target_roi_bottom:
            raise ValueError("监测区域左上坐标必须小于右下坐标")
        if self.min_optical_zoom > self.max_optical_zoom:
            raise ValueError("最小光学倍率不能大于最大倍率")
        if self.recording_mode == "optical_cycle" and self.zoom_factor <= 1:
            raise ValueError("光学循环的目标倍率必须大于 1×")
        if self.recording_mode == "passage" and self.zoom_factor > 8:
            raise ValueError("数字取景模式的放大倍率不能超过 8×")
        if self.pre_roll_seconds >= self.max_clip_seconds:
            raise ValueError("预录时长必须小于单段录像时长")
        return self
