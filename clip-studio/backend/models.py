from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CameraKeyframe(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    time: float = Field(ge=0)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    zoom: float = Field(ge=1, le=3)
    transition: Literal["cut", "linear"] = "cut"


class Clip(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    asset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    label: str = Field(default="Фрагмент", max_length=80)
    crop_x: float = Field(default=0.5, ge=0, le=1)
    camera: list[CameraKeyframe] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start or self.end - self.start > 180:
            raise ValueError("Фрагмент должен длиться от 0 до 180 секунд")
        previous = -1.0
        for keyframe in self.camera:
            if keyframe.time <= previous:
                raise ValueError("Точки кадрирования должны иметь уникальное возрастающее время")
            if keyframe.time > self.end - self.start:
                raise ValueError("Точка кадрирования выходит за длительность фрагмента")
            previous = keyframe.time
        return self


class Box(BaseModel):
    x: float = Field(ge=0, le=.97)
    y: float = Field(ge=0, le=.98)
    w: float = Field(ge=.03, le=1)
    h: float = Field(ge=.02, le=1)

    @model_validator(mode="after")
    def within_canvas(self):
        if self.x + self.w > 1.001 or self.y + self.h > 1.001:
            raise ValueError("Слой выходит за границы кадра")
        return self


class Style(BaseModel):
    layout: Literal["plain", "card"] = "plain"
    aspect: Literal["9:16", "1:1", "16:9"] = "9:16"
    fit: Literal["cover", "contain"] = "cover"
    border: Literal[0, 24, 48] = 0
    background: str = Field(default="#101116", pattern=r"^#[a-fA-F0-9]{6}$")
    corner_radius: int = Field(default=0, ge=0, le=160)
    header_height: int = Field(default=300, ge=160, le=560)
    account: str = Field(default="", max_length=50)
    eyebrow: str = Field(default="", max_length=60)
    text_color: str = Field(default="#ffffff", pattern=r"^#[a-fA-F0-9]{6}$")
    font: Literal["manrope", "montserrat", "russoone", "lora", "robotomono", "cormorantgaramond", "cormorantitalic", "prata", "tenorsans", "poiretone", "greatvibes", "badscript", "unbounded"] = "manrope"
    header_image: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    video_box: Box | None = None
    header_box: Box | None = None
    title_box: Box | None = None
    eyebrow_box: Box | None = None
    account_box: Box | None = None
    title: str = Field(default="", max_length=160)
    title_position: Literal["top", "center", "bottom"] = "bottom"
    font_size: int = Field(default=48, ge=24, le=72)
    subtitles: bool = False
    mute: bool = False


class Project(BaseModel):
    name: str = Field(default="Мой первый ролик", min_length=1, max_length=100)
    color_mode: Literal["sdr", "hdr"] = "sdr"
    clips: list[Clip] = Field(default_factory=list, max_length=30)
    style: Style = Field(default_factory=Style)
    caption: str = Field(default="", max_length=2200)

    @model_validator(mode="after")
    def total_duration(self):
        if sum(c.end - c.start for c in self.clips) > 180:
            raise ValueError("Общая длительность ролика — не больше 180 секунд")
        return self


class ExportRequest(BaseModel):
    project: Project
    mode: Literal["sdr", "hdr"] = "sdr"
    resolution: Literal[720, 1080] = 1080

    @model_validator(mode="before")
    @classmethod
    def default_mode_from_project(cls, value):
        if not isinstance(value, dict) or "mode" in value:
            return value
        project = value.get("project")
        project_mode = project.get("color_mode") if isinstance(project, dict) else getattr(project, "color_mode", None)
        if project_mode in {"sdr", "hdr"}:
            return {**value, "mode": project_mode}
        return value


class YoutubeRequest(BaseModel):
    url: str = Field(max_length=500)
    max_height: Literal[360, 480, 720, 1080, 2160] = 1080
    prefer_hdr: bool = False
    analyze: bool = True


class AnalysisRequest(BaseModel):
    provider: Literal["openai", "gemini", "groq"] = "groq"
    focus: str = Field(default="", max_length=800)
    start: float = Field(default=0, ge=0)
    end: float = Field(gt=0)
    clip_seconds: int = Field(default=60, ge=15, le=90)

    @model_validator(mode="after")
    def window(self):
        if self.end <= self.start or self.end - self.start > 1800:
            raise ValueError("Выберите участок для анализа до 30 минут")
        return self


class AISettings(BaseModel):
    provider: Literal["openai", "gemini", "groq"] = "groq"
    openai_key: str = Field(default="", max_length=500)
    gemini_key: str = Field(default="", max_length=500)
    groq_key: str = Field(default="", max_length=500)
    openai_model: str = Field(default="gpt-4.1-mini", pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    gemini_model: str = Field(default="gemini-3.8-flash", pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    groq_model: str = Field(default="openai/gpt-oss-20b", pattern=r"^[a-zA-Z0-9/._-]{1,100}$")
    auto_analyze: bool = False
