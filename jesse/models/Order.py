from playhouse.postgres_ext import *

import jesse.helpers as jh
import jesse.services.logger as logger
import jesse.services.selectors as selectors
from jesse.config import config
from jesse.services.notifier import notify
from jesse.enums import order_statuses, order_submitted_via
from jesse.services.db import database


if database.is_closed():
    database.open_connection()


class Order(Model):
    # id generated by Jesse for database usage
    id = UUIDField(primary_key=True)
    trade_id = UUIDField(index=True, null=True)
    session_id = UUIDField(index=True)

    # id generated by market, used in live-trade mode
    exchange_id = CharField(null=True)
    # some exchanges might require even further info
    vars = JSONField(default={})
    symbol = CharField()
    exchange = CharField()
    side = CharField()
    type = CharField()
    reduce_only = BooleanField()
    qty = FloatField()
    filled_qty = FloatField(default=0)
    price = FloatField(null=True)
    status = CharField(default=order_statuses.ACTIVE)
    created_at = BigIntegerField()
    executed_at = BigIntegerField(null=True)
    canceled_at = BigIntegerField(null=True)

    # needed in Jesse, but no need to store in database(?)
    submitted_via = None

    class Meta:
        from jesse.services.db import database

        database = database.db
        indexes = ((('trade_id', 'exchange', 'symbol', 'status', 'created_at'), False),)

    def __init__(self, attributes: dict = None, **kwargs) -> None:
        Model.__init__(self, attributes=attributes, **kwargs)

        if attributes is None:
            attributes = {}

        for a, value in attributes.items():
            setattr(self, a, value)

        if self.created_at is None:
            self.created_at = jh.now_to_timestamp()

        # if jh.is_live():
        #     from jesse.store import store
            # self.session_id = store.app.session_id
            # self.save(force_insert=True)

        if jh.is_live():
            self.notify_submission()

        if jh.is_debuggable('order_submission') and (self.is_active or self.is_queued):
            txt = f'{"QUEUED" if self.is_queued else "SUBMITTED"} order: {self.symbol}, {self.type}, {self.side}, {self.qty}'
            if self.price:
                txt += f', ${round(self.price, 2)}'
            logger.info(txt)

        # handle exchange balance for ordered asset
        e = selectors.get_exchange(self.exchange)
        e.on_order_submission(self)

    def notify_submission(self) -> None:
        if config['env']['notifications']['events']['submitted_orders'] and (self.is_active or self.is_queued):
            txt = f'{"QUEUED" if self.is_queued else "SUBMITTED"} order: {self.symbol}, {self.type}, {self.side}, {self.qty}'
            if self.price:
                txt += f', ${round(self.price, 2)}'
            notify(txt)

    @property
    def is_canceled(self) -> bool:
        return self.status == order_statuses.CANCELED

    @property
    def is_active(self) -> bool:
        return self.status == order_statuses.ACTIVE

    @property
    def is_cancellable(self):
        """
        orders that are either active or partially filled
        """
        return self.is_active or self.is_partially_filled

    @property
    def is_queued(self) -> bool:
        """
        Used in live mode only: it means the strategy has considered the order as submitted,
        but the exchange does not accept it because of the distance between the current
        price and price of the order. Hence it's been queued for later submission.

        :return: bool
        """
        return self.status == order_statuses.QUEUED

    @property
    def is_new(self) -> bool:
        return self.is_active

    @property
    def is_executed(self) -> bool:
        return self.status == order_statuses.EXECUTED

    @property
    def is_filled(self) -> bool:
        return self.is_executed

    @property
    def is_partially_filled(self) -> bool:
        return self.status == order_statuses.PARTIALLY_FILLED

    @property
    def is_stop_loss(self):
        return self.submitted_via == order_submitted_via.STOP_LOSS

    @property
    def is_take_profit(self):
        return self.submitted_via == order_submitted_via.TAKE_PROFIT

    @property
    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'exchange_id': self.exchange_id,
            'symbol': self.symbol,
            'side': self.side,
            'type': self.type,
            'qty': self.qty,
            'filled_qty': self.filled_qty,
            'price': self.price,
            'status': self.status,
            'created_at': self.created_at,
            'canceled_at': self.canceled_at,
            'executed_at': self.executed_at,
        }

    @property
    def position(self):
        return selectors.get_position(self.exchange, self.symbol)

    @property
    def value(self) -> float:
        return abs(self.qty) * self.price

    @property
    def remaining_qty(self) -> float:
        return jh.prepare_qty(abs(self.qty) - abs(self.filled_qty), self.side)

    def queue(self):
        self.status = order_statuses.QUEUED
        self.canceled_at = None
        if jh.is_debuggable('order_submission'):
            txt = f'QUEUED order: {self.symbol}, {self.type}, {self.side}, {self.qty}'
            if self.price:
                txt += f', ${round(self.price, 2)}'
                logger.info(txt)
        self.notify_submission()

    def resubmit(self):
        # don't allow resubmission if the order is already active or cancelled
        if not self.is_queued:
            raise NotSupportedError(f'Cannot resubmit an order that is not queued. Current status: {self.status}')

        # regenerate the order id to avoid errors on the exchange's side
        self.id = jh.generate_unique_id()
        self.status = order_statuses.ACTIVE
        self.canceled_at = None
        if jh.is_debuggable('order_submission'):
            txt = f'SUBMITTED order: {self.symbol}, {self.type}, {self.side}, {self.qty}'
            if self.price:
                txt += f', ${round(self.price, 2)}'
                logger.info(txt)
        self.notify_submission()

    def cancel(self, silent=False, source='') -> None:
        if self.is_canceled or self.is_executed:
            return

        # to fix when the cancelled stream's lag causes cancellation of queued orders
        if source == 'stream' and self.is_queued:
            return

        self.canceled_at = jh.now_to_timestamp()
        self.status = order_statuses.CANCELED

        # if jh.is_live():
        #     self.save()

        if not silent:
            txt = f'CANCELED order: {self.symbol}, {self.type}, {self.side}, {self.qty}'
            if self.price:
                txt += f', ${round(self.price, 2)}'
            if jh.is_debuggable('order_cancellation'):
                logger.info(txt)
            if jh.is_live():
                if config['env']['notifications']['events']['cancelled_orders']:
                    notify(txt)

        # handle exchange balance
        e = selectors.get_exchange(self.exchange)
        e.on_order_cancellation(self)

    def execute(self, silent=False) -> None:
        if self.is_canceled or self.is_executed:
            return

        self.executed_at = jh.now_to_timestamp()
        self.status = order_statuses.EXECUTED

        # if jh.is_live():
        #     self.save()

        if not silent:
            txt = f'EXECUTED order: {self.symbol}, {self.type}, {self.side}, {self.qty}'
            if self.price:
                txt += f', ${round(self.price, 2)}'
            # log
            if jh.is_debuggable('order_execution'):
                logger.info(txt)
            # notify
            if jh.is_live():
                if config['env']['notifications']['events']['executed_orders']:
                    notify(txt)

        # log the order of the trade for metrics
        from jesse.store import store
        store.completed_trades.add_executed_order(self)

        # handle exchange balance for ordered asset
        e = selectors.get_exchange(self.exchange)
        e.on_order_execution(self)

        p = selectors.get_position(self.exchange, self.symbol)
        if p:
            p._on_executed_order(self)

    def execute_partially(self, silent=False) -> None:
        self.executed_at = jh.now_to_timestamp()
        self.status = order_statuses.PARTIALLY_FILLED

        # if jh.is_live():
        #     self.save()

        if not silent:
            txt = f"PARTIALLY FILLED: {self.symbol}, {self.type}, {self.side}, filled qty: {self.filled_qty}, remaining qty: {self.remaining_qty}, price: {self.price}"
            # log
            if jh.is_debuggable('order_execution'):
                logger.info(txt)
            # notify
            if jh.is_live():
                if config['env']['notifications']['events']['executed_orders']:
                    notify(txt)

        # log the order of the trade for metrics
        from jesse.store import store
        store.completed_trades.add_executed_order(self)

        p = selectors.get_position(self.exchange, self.symbol)

        if p:
            p._on_executed_order(self)


# if database is open, create the table
if database.is_open():
    Order.create_table()
