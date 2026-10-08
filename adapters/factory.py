# -*- coding: utf-8 -*-
"""适配器工厂"""
from typing import List
from adapters.base import CrawlerAdapter
from adapters.all_adapters import (
    MeituanAdapter, YuntaiAdapter, LeyaoyaoAdapter, DuojinbaoAdapter,
    JingjianAdapter, StarThingAdapter, NewSystemAdapter, HuilianAdapter,
    KPayAdapter, OctopusAdapter, PaymentAdapter, DouyinAdapter,
    CoinExchangeAdapter, YoucaihuaAdapter,
)

def build_adapters() -> List[CrawlerAdapter]:
    return [
        MeituanAdapter(), YuntaiAdapter(), LeyaoyaoAdapter(), DuojinbaoAdapter(),
        JingjianAdapter(), StarThingAdapter(), NewSystemAdapter(), HuilianAdapter(),
        KPayAdapter(), OctopusAdapter(), PaymentAdapter(), DouyinAdapter(),
        CoinExchangeAdapter(), YoucaihuaAdapter(),
    ]
