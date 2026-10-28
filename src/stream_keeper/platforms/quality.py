"""Platform-native quality identifiers and labels used by the task UI."""

QUALITY_OPTIONS = {
    "douyin": (
        ("best", "最高可用画质"), ("origin", "原画"), ("uhd", "蓝光"),
        ("fhd", "蓝光 FHD"), ("hd", "超清"), ("sd", "高清"), ("ld", "标清"), ("md", "流畅"),
    ),
    "bilibili": (
        ("best", "最高可用画质"), ("30000", "杜比"), ("20000", "4K"), ("15000", "2K"),
        ("10000", "原画"), ("400", "蓝光"), ("250", "超清"), ("150", "高清"), ("80", "流畅"),
    ),
    "kuaishou": (
        ("best", "最高可用画质"), ("WQHD_2K", "2K"), ("BLUE_RAY", "蓝光质臻"),
        ("SUPER", "蓝光"), ("HIGH", "超清"), ("STANDARD", "高清"),
    ),
}


def validate_quality(platform: str, quality: str) -> str:
    value = quality.strip()
    if value not in dict(QUALITY_OPTIONS[platform]):
        raise ValueError(f"不支持的画质 {quality!r}，请使用该平台的画质选项")
    return value


def quality_label(platform: str, quality: str) -> str:
    return dict(QUALITY_OPTIONS[platform]).get(quality, quality)
