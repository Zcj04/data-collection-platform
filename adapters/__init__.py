# -*- coding: utf-8 -*-
"""适配器层 - 统一封装各平台爬虫，对外暴露 run(date) 接口"""

from adapters.base import CrawlerAdapter, CrawlerError
