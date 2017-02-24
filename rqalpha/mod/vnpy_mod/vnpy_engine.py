# -*- coding: utf-8 -*-
from dateutil.parser import parse
from Queue import Queue
import numpy as np

from rqalpha.const import SIDE, ORDER_TYPE, POSITION_EFFECT
from rqalpha.model.trade import Trade
from rqalpha.events import EVENT
from rqalpha.utils import get_account_type

from .vn_trader.eventEngine import EventEngine2
from .vn_trader.vtGateway import VtOrderReq, VtCancelOrderReq, VtSubscribeReq
from .vn_trader.eventType import EVENT_CONTRACT, EVENT_ORDER, EVENT_TRADE, EVENT_TICK, EVENT_LOG
from .vn_trader.vtConstant import DIRECTION_LONG, DIRECTION_SHORT
from .vn_trader.vtConstant import PRICETYPE_LIMITPRICE, PRICETYPE_MARKETPRICE
from .vn_trader.vtConstant import OFFSET_CLOSE, OFFSET_OPEN
from .vn_trader.vtConstant import STATUS_NOTTRADED, STATUS_PARTTRADED, STATUS_ALLTRADED, STATUS_CANCELLED

from .vn_trader.vtConstant import CURRENCY_CNY
from .vn_trader.vtConstant import PRODUCT_FUTURES

SIDE_MAPPING = {
    SIDE.BUY: DIRECTION_LONG,
    SIDE.SELL: DIRECTION_SHORT
}

ORDER_TYPE_MAPPING = {
    ORDER_TYPE.MARKET: PRICETYPE_MARKETPRICE,
    ORDER_TYPE.LIMIT: PRICETYPE_LIMITPRICE
}

POSITION_EFFECT_MAPPING = {
    POSITION_EFFECT.OPEN: OFFSET_OPEN,
    POSITION_EFFECT.CLOSE: OFFSET_CLOSE,
}

_engine = None


def _order_book_id(symbol):
    if len(symbol) < 4:
        return None
    if symbol[-4] not in '0123456789':
        order_book_id = symbol[:2] + '1' + symbol[-3:]
    else:
        order_book_id = symbol
    return order_book_id.upper()


class RQVNPYEngine(object):
    def __init__(self, env):
        self._env = env
        self.event_engine = EventEngine2()
        self.event_engine.start()

        self.vnpy_gateway = None

        self._init_gateway()

        self._order_dict = {}
        self._vnpy_order_dict = {}
        self._open_order_dict = {}
        self._trade_dict = {}
        self._contract_dict = {}
        self._tick_que = Queue()

        self._register_event()

    @property
    def open_orders(self):
        return list(self._open_order_dict.values())

    def on_order(self, event):
        vnpy_order = event.dict_['data']
        vnpy_order_id = vnpy_order.vtOrderID

        try:
            order = self._order_dict[vnpy_order_id]
        except KeyError:
            print('No Such order in rqalpha query.')
            return

        account = self._get_account_for(order)

        order._activate()

        self._env.event_bus.publish_event(EVENT.ORDER_CREATION_PASS, account, order)

        self._vnpy_order_dict[order.order_id] = vnpy_order
        if vnpy_order.status == STATUS_NOTTRADED or vnpy_order.status == STATUS_PARTTRADED:
            self._open_order_dict[vnpy_order_id] = order
        elif vnpy_order.status == STATUS_ALLTRADED:
            if vnpy_order_id in self._open_order_dict:
                del self._open_order_dict[vnpy_order_id]
        elif vnpy_order.status == STATUS_CANCELLED:
            if vnpy_order_id in self._open_order_dict:
                del self._open_order_dict[vnpy_order_id]
            order._mark_rejected('Order was rejected or cancelled by vnpy.')

    def on_trade(self, event):
        vnpy_trade = event.dict_['data']
        order = self._order_dict[vnpy_trade.vtOrderID]
        account = self._get_account_for(order)
        ct_amount = account.portfolio.positions[order.order_book_id]._cal_close_today_amount(vnpy_trade.volume,
                                                                                             order.side)
        trade = Trade.__from_create__(
            order=order,
            calendar_dt=order.datetime,
            trading_dt=order.trading_datetime,
            price=vnpy_trade.price,
            amount=vnpy_trade.volume,
            close_today_amount=ct_amount
        )
        trade._commission = account.commission_decider.get_commission(trade)
        trade._tax = account.tax_decider.get_tax(trade)
        order._fill(trade)
        self._env.event_bus.publish_event(EVENT.TRADE, account, trade)

    def on_contract(self, event):
        contract = event.dict_['data']
        order_book_id = _order_book_id(contract.symbol)
        self._contract_dict[order_book_id] = contract

    def on_tick(self, event):
        vnpy_tick = event.dict_['data']
        print vnpy_tick.__dict__
        tick = {
            'order_book_id': _order_book_id(vnpy_tick.symbol),
            'datetime': parse('%s %s' % (vnpy_tick.date, vnpy_tick.time)),
            'open': vnpy_tick.openPrice,
            'last': vnpy_tick.lastPrice,
            'low': vnpy_tick.lowPrice,
            'high': vnpy_tick.highPrice,
            'prev_close': vnpy_tick.preClosePrice,
            'volume': vnpy_tick.volume,
            'total_turnover': np.nan,
            'open_interest': vnpy_tick.openInterest,
            'prev_settlement': np.nan,

            'bid': [
                vnpy_tick.bidPrice1,
                vnpy_tick.bidPrice2,
                vnpy_tick.bidPrice3,
                vnpy_tick.bidPrice4,
                vnpy_tick.bidPrice5,
            ],
            'bid_volume': [
                vnpy_tick.bidVolume1,
                vnpy_tick.bidVolume2,
                vnpy_tick.bidVolume3,
                vnpy_tick.bidVolume4,
                vnpy_tick.bidVolume5,
            ],
            'ask': [
                vnpy_tick.askPrice1,
                vnpy_tick.askPrice2,
                vnpy_tick.askPrice3,
                vnpy_tick.askPrice4,
                vnpy_tick.askPrice5,
            ],
            'ask_volume': [
                vnpy_tick.askVolume1,
                vnpy_tick.askVolume2,
                vnpy_tick.askVolume3,
                vnpy_tick.askVolume4,
                vnpy_tick.askVolume5,
            ],

            'limit_up': vnpy_tick.upperLimit,
            'limit_down': vnpy_tick.lowerLimit,
        }
        self._tick_que.put(tick)

    def on_log(self, event):
        log = event.dict_['data']
        # TODO: 调用rqalpha logger 模块
        print(log.logContent)

    def on_universe_changed(self, universe):
        for order_book_id in universe:
            # TODO need mapping
            self.subscribe(order_book_id)

    def connect(self):
        login_dict = {
            'userID': self._env.config.mod.vnpy.ctp.userID,
            'password': self._env.config.mod.vnpy.ctp.password,
            'brokerID': self._env.config.mod.vnpy.ctp.brokerID,
            'tdAddress': self._env.config.mod.vnpy.ctp.tdAddress,
            'mdAddress': self._env.config.mod.vnpy.ctp.mdAddress,
        }
        self.vnpy_gateway.connect(login_dict)

    def send_order(self, order):
        account = self._get_account_for(order)
        self._env.event_bus.publish_event(EVENT.ORDER_PENDING_NEW, account, order)

        account.append_order(order)

        contract = self._get_contract_from_order_book_id(order.order_book_id)
        if contract is None:
            order._mark_cancelled('No contract exists whose order_book_id is %s' % order.order_book_id)

        if order._is_final():
            return

        order_req = VtOrderReq()
        order_req.symbol = contract.symbol
        order_req.exchange = contract.exchange
        order_req.price = order.price
        order_req.volume = order.quantity
        order_req.direction = SIDE_MAPPING[order.side]
        order_req.priceType = ORDER_TYPE_MAPPING[order.type]
        order_req.offset = POSITION_EFFECT[order.position_effect]
        order_req.currency = CURRENCY_CNY
        order_req.productClass = PRODUCT_FUTURES

        vnpy_order_id = self.vnpy_gateway.sendOrder(order_req)
        self._order_dict[vnpy_order_id] = order

    def cancel_order(self, order):
        account = self._get_account_for(order)
        self._env.event_bus.publish_event(EVENT.ORDER_PENDING_CANCEL, account, order)

        vnpy_order = self._vnpy_order_dict[order.order_id]

        cancel_order_req = VtCancelOrderReq()
        cancel_order_req.symbol = vnpy_order.symbol
        cancel_order_req.exchange = vnpy_order.exchange
        cancel_order_req.sessionID = vnpy_order.sessionID
        cancel_order_req.orderID = vnpy_order.orderID

        self.vnpy_gateway.cancelOrder(cancel_order_req)

    def subscribe(self, order_book_id):
        contract = self._get_contract_from_order_book_id(order_book_id)
        if contract is None:
            return
        subscribe_req = VtSubscribeReq()
        subscribe_req.symbol = contract.symbol
        subscribe_req.exchange = contract.exchange
        subscribe_req.productClass = PRODUCT_FUTURES
        subscribe_req.currency = CURRENCY_CNY
        self.vnpy_gateway.subscribe(subscribe_req)

    def get_tick(self):
        return self._tick_que.get(block=True)

    def exit(self):
        self.vnpy_gateway.close()
        self.event_engine.stop()

    def _init_gateway(self):
        gateway_name = self._env.config.mod.vnpy.gateway_name
        if gateway_name == 'CTP':
            try:
                from .vn_trader.ctpGateway.ctpGateway import CtpGateway
                self.vnpy_gateway = CtpGateway(self.event_engine, 'CTP')
                self.vnpy_gateway.setQryEnabled(True)
            except ImportError:
                print('No Gateway named CTP')
        else:
            print('No Gateway named %s' % gateway_name)

    def _register_event(self):
        self.event_engine.register(EVENT_ORDER, self.on_order)
        self.event_engine.register(EVENT_CONTRACT, self.on_contract)
        self.event_engine.register(EVENT_TRADE, self.on_trade)
        self.event_engine.register(EVENT_TICK, self.on_tick)
        self.event_engine.register(EVENT_LOG, self.on_log)

        self._env.event_bus.add_listener(EVENT.POST_UNIVERSE_CHANGED, self.on_universe_changed)

    def _get_contract_from_order_book_id(self, order_book_id):
        try:
            return self._contract_dict[order_book_id]
        except KeyError:
            print('No such contract whose order_book_id is %s ' % order_book_id)

    def _get_account_for(self, order):
        account_type = get_account_type(order.order_book_id)
        return self._env.broker.get_account()[account_type]
