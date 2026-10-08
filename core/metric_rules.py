"""采集入库与质量提示共用的业务指标规则。"""


def allows_decrease(platform: str, metric: str, current: float) -> bool:
    """货款、芸苔远程取币、美团手续费和负数属于允许的业务数据。

    美团手续费 = 美团收款 - 美团实收（结算价）。美团会不定期在"其他调整"
    中批量返还款项（如技术服务费/营销费用返还），这些行只增加结算价、
    不增加总收入，导致累计手续费合法回退；收款与实收本身仍受单调校验保护。
    """
    return (
        platform == "payment"
        or "货款" in str(metric)
        or (platform == "yuntai" and metric == "芸苔远程取币")
        or (platform == "meituan" and str(metric) == "美团手续费")
        or current < 0
    )
