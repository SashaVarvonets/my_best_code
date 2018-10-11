from datetime import datetime, timedelta
from pprint import pprint
import base64
import logging
import json
import requests

from openerp import api, models, fields
from openerp.addons.cb_website.controllers.controllers import filter_offers, get_addresses
from openerp.exceptions import UserError
from openerp.tools.translate import _

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # Delivery details
    delivery_order_booked = fields.Boolean(default=False)
    delivery_order_uuid = fields.Char(string='Delivery UUID', default=None)
    delivery_label = fields.Binary(attachment=True, store=True)
    delivery_label_fname = fields.Char(string='Name', size=64)
    collection_date = fields.Date(string='Collection date', default=None)
    current_delivery_status = fields.Char(string='Current status of delivery', default=None)
    delivery_price = fields.Char(string='Price of delivery', default=None)

    # Return details
    return_order_booked = fields.Boolean(default=False)
    return_order_uuid = fields.Char(string='Return UUID', default=None)
    return_delivery_label = fields.Binary(attachment=True)
    current_return_status = fields.Char(string='Current status of return delivery',default=None)
    return_price = fields.Char(string='Price of return delivery', default=None)


    @api.model
    def create_myflyingbox_order(self):
        lines_ids = self.env['sale.order.line'].sudo().search([('start_date', '!=', False)])
        company = self.env['res.company'].sudo().search([('name', '=', 'Louerphotobooth')])

        today = datetime.now()
        one_day = timedelta(days=1)
        week = timedelta(weeks=2)

        url_quote = "https://api.myflyingbox.com/v2/quotes"
        # url_quote = "https://test.myflyingbox.com/v2/quotes"
        url_offer = "https://api.myflyingbox.com/v2/offers/"
        # url_offer = "https://test.myflyingbox.com/v2/offers/"
        url_order = "https://api.myflyingbox.com/v2/orders"
        # url_order = "https://test.myflyingbox.com/v2/orders"

        mfb_carrier = self.env["delivery.carrier"].sudo().search([("delivery_type", "=", 'mfb')], limit=1)
        login = mfb_carrier.mfb_api_login
        password = mfb_carrier.mfb_api_password
        # password = "f3kVr4UsFtmVAxxKx1szBXRDbyFcGSsq"  # password for test API server
        auth = (login, password)

        filtered_lines = lines_ids.filtered(lambda r: (fields.Datetime.from_string(r.start_date) - today) <= week
                                                      and r.order_id.carrier_id.id == self.env.ref('myflyingbox.mfb_record').id
                                                      and not r.order_id.delivery_order_booked
                                                      and r.order_id.state in ['sale', 'done'])

        for line in filtered_lines:
            _logger.info('MyFlyingBox: Try to create delivery and return orders for sale order %s' % line.order_id.name)

            product = line.product_id.rented_product_id
            if not product:
                _logger.error('Product for rent is not found')
                continue

            data_for_quote = {}
            try:
                data_for_quote = json.dumps(self.prepare_shipment_information_to_get_quote(line, product))
            except Exception as e:
                _logger.error(e.message)
            if not data_for_quote:
                continue

            req = requests.post(url_quote, auth=auth, data=data_for_quote)

            res = {}
            try:
                res = json.loads(req.text)
            except Exception as e:
                _logger.error(e.message)

            if not res:
                continue

            if res['status'] == 'failure':
                _logger.error(pprint(res))
                continue

            data_offers = res["data"]["offers"]

            new_offers = filter_offers(data_offers, direction='forward')
            if not new_offers:
                _logger.error("No proper matches for placing delivery order")
                continue

            offers_id = new_offers[0]["id"]

            # handle pick up date
            available_collection_dates = new_offers[0].get('collection_dates', [])
            event_date = fields.Datetime.from_string(line.start_date)
            delivery_date = event_date - one_day  # 1 day before event

            if delivery_date.weekday() == 5: # checks if it's Saturday, makes Friday
                delivery_date -= one_day
            elif delivery_date.weekday() == 6: # check if it's Sanday, makes Friday
                delivery_date -= one_day * 2

            delay = new_offers[0].get('product', {}).get('delay', '')
            delay_days = (int(delay.split('-')[-1]) / 24) + 1  # 1 day reserve
            delay_timedelta = timedelta(days=delay_days)
            collection_date = delivery_date - delay_timedelta


            if collection_date.weekday() == 5:  # checks if it's Saturday, makes Friday
                collection_date -= one_day
            elif collection_date.weekday() == 6:  # check if it's Sanday, makes Friday
                collection_date -= one_day * 2

            filtered_collection_dates = filter(lambda date: date[u'date'] == collection_date.strftime('%Y-%m-%d'),
                                               available_collection_dates)
            collection_date = filtered_collection_dates[0][u'date'] if filtered_collection_dates else ''

            data_event_address = {
                'location[street]': line.order_id.partner_event_id.street,
                'location[city]': line.order_id.partner_event_id.city
            }

            req = requests.get(
                url_offer + offers_id + "/available_delivery_locations", auth=auth, data=data_event_address
            )

            res = {}
            try:
                res = json.loads(req.text)
            except Exception as e:
                _logger.error(e.message)
            if not res:
                continue

            deliveries = res.get('data', [])

            addresses = get_addresses(deliveries)
            if addresses > 9:
                addresses = addresses[0:9]

            selected_delivery_address = {
                "company": line.order_id.partner_shipping_id.name,
                "street": line.order_id.partner_shipping_id.street,
                "city": line.order_id.partner_shipping_id.city,
                "postal_code": line.order_id.partner_shipping_id.zip,
            }

            location_code = None
            for address in addresses:
                if address["company"] == selected_delivery_address["company"] \
                        and address["city"] == selected_delivery_address["city"] \
                        and address["postal_code"] == selected_delivery_address["postal_code"]:
                    location_code = address["code"]
                    break

            if location_code is None:
                _logger.error("there is no address")
                continue

            data_for_order = {}
            try:
                data_for_order = json.dumps(self.prepare_shipment_information_to_place_an_order(
                    line, offers_id, location_code, product, company, collection_date))
            except Exception as e:
                _logger.error(e.message)
            if not data_for_order:
                continue
            continue

            req = requests.post(url_order, auth=auth, data=data_for_order)

            res = {}
            try:
                res = json.loads(req.text)
            except Exception as e:
                _logger.error(e.message)
            if not res:
                continue

            if res['status'] == 'failure':
                _logger.error(pprint(res))
                continue


            # Display delivery details
            line.order_id.delivery_order_uuid = res["data"]["id"]
            line.order_id.delivery_order_booked = True
            line.order_id.collection_date = collection_date
            line.order_id.delivery_price = res["data"]["price"]["amount"]
            _logger.info("Delivery order is placed")

            # ---------------------------------------------------------------------
            data_for_quote = {}
            try:
                data_for_quote = json.dumps(self.prepare_shipment_information_to_get_quote_to_return(line, product))
            except Exception as e:
                _logger.error(e.message)
            if not data_for_quote:
                continue

            req = requests.post(url_quote, auth=auth, data=data_for_quote)

            res = {}
            try:
                res = json.loads(req.text)
            except Exception as e:
                _logger.error(e.message)
            if not res:
                _logger.error("t")
                continue

            data_offers = res["data"]["offers"]

            new_offers = filter_offers(data_offers, direction='back')
            if not new_offers:
                _logger.error("No proper matches for placing return order")
                continue
            offers_id = new_offers[0]["id"]

            data_for_order = {}
            try:
                data_for_order = json.dumps(
                    self.prepare_shipment_information_to_place_an_order_to_return(line, offers_id))
            except Exception as e:
                _logger.error(e.message)
            if not data_for_order:
                continue

            req = requests.post(url_order, auth=auth, data=data_for_order)

            res = {}
            try:
                res = json.loads(req.text)
            except Exception as e:
                _logger.error(e.message)
            if not res:
                continue

            line.order_id.return_order_uuid = res["data"]["id"]
            line.order_id.return_order_booked = True
            line.order_id.return_price = res["data"]["total_price"]["amount"]
            _logger.info("Return order is placed")

    def prepare_shipment_information_to_get_quote(self, line, product):
        shipper = {
            "country": line.order_id.carrier_id.country_id.code,
            "postal_code": str(line.order_id.carrier_id.postal_code),
            "city": line.order_id.carrier_id.city,
        }

        recipient = {
            "is_a_company": False,
            "country": line.order_id.partner_id.country_id.code,
            "postal_code": line.order_id.partner_id.zip,
            "city": line.order_id.partner_id.city,
        }

        parcels = [{
            "weight": product.weight,
            "length": product.length,
            "width": product.width,
            "height": product.height,
        }]

        context = {"quote": {
            "shipper": shipper,
            "recipient": recipient,
            "parcels": parcels},
        }
        return context

    def prepare_shipment_information_to_place_an_order(
            self, line, offer_id, location_code, product, company, collection_date):
        # dif_day = timedelta(days=4)
        # start_date = fields.Datetime.to_string(fields.Date.from_string(line.start_date) - dif_day)
        shipper = {
            # "company":         'company name',
            "name": line.order_id.carrier_id.shipper_name,
            "street": line.order_id.carrier_id.street,
            # "state":           'state',  # line.order_id.carrier_id.state_id.name,
            "phone": line.order_id.carrier_id.phone_number,
            "email": line.order_id.carrier_id.email,
            "collection_date": collection_date,
        }

        recipient = {
            "name": line.order_id.partner_id.name,
            # "company":         'company name',
            "location_code": location_code,
            "street": line.order_id.partner_id.street,
            # "state":           'state',  # line.order_id.partner_event_id.state_id.name,
            "phone": line.order_id.partner_id.phone,
            "email": line.order_id.partner_id.email,
        }

        parcels = [{
            'value': product.list_price,
            'currency': company.currency_id.name,
            'description': product.name,
            'country_of_origin': line.order_id.carrier_id.country_id.code
        }]

        context = {"order": {
            "offer_id": offer_id,
            "shipper": shipper,
            "recipient": recipient,
            "parcels": parcels},
        }
        return context

    def prepare_shipment_information_to_get_quote_to_return(self, line, product):
        shipper = {
            "country": line.order_id.partner_id.country_id.code,
            "postal_code": str(line.order_id.partner_id.zip),
            "city": line.order_id.partner_id.city,
        }

        recipient = {
            "is_a_company": False,
            "country": line.order_id.carrier_id.country_id.code,
            "postal_code": line.order_id.carrier_id.postal_code,
            "city": line.order_id.carrier_id.city,
        }

        parcels = [{
            "weight": product.weight,
            "length": product.length,
            "width": product.width,
            "height": product.height,
        }]

        context = {"quote": {
            "shipper": shipper,
            "recipient": recipient,
            "parcels": parcels},
        }
        return context

    def prepare_shipment_information_to_place_an_order_to_return(self, line, offers_id):
        one_day = timedelta(days=1)
        start_date = fields.Datetime.to_string(fields.Datetime.from_string(line.start_date) + one_day)
        shipper = {
            # "company":         'company name',
            "name": line.order_id.partner_id.name,
            "street": line.order_id.partner_id.street,
            # "state":           'state',
            "phone": line.order_id.partner_id.phone,
            "email": line.order_id.partner_id.email,
            "collection_date": start_date,
        }

        recipient = {
            "name": line.order_id.carrier_id.shipper_name,
            # "company":         'company name',
            "street": line.order_id.carrier_id.street,
            # "state":           'state',
            "phone": line.order_id.carrier_id.phone_number,
            "email": line.order_id.carrier_id.email,
        }

        parcels = [{
            'value': '10',
            'currency': 'EUR',
            'description': 'description',
            'country_of_origin': 'FR'
        }]

        context = {"order": {
            "offer_id": offers_id,
            "shipper": shipper,
            "recipient": recipient,
            "parcels": parcels},
        }
        return context

    @api.multi
    def request_to_download_delivery_label(self):
        if not self.delivery_order_uuid:
            raise UserError(_('No order exist'))
        context = {
            'type': 'ir.actions.act_url',
            'url': '/web/binary/download_document?model=sale.order&type=delivery&id=%s&filename=%s.pdf' %
                   (self.id, 'Label for delivery order:' + self.name),
            'target': 'blank',
        }
        return context

    @api.multi
    def request_to_download_return_label(self):
        if not self.return_order_uuid:
            raise UserError(_('No order exist'))
        context = {
            'type': 'ir.actions.act_url',
            'url': '/web/binary/download_document?model=sale.order&type=return&id=%s&filename=%s.pdf' % (
                self.id, 'Label for return order:' + self.name),
            'target': 'blank',
        }
        return context

    @api.multi
    def request_to_track_parcel(self, order_id=None):
        # Request to track parcel from MFB
        url_orders = "https://api.myflyingbox.com/v2/orders/"
        # url_orders = "https://test.myflyingbox.com/v2/orders/"

        mfb_carrier = self.env["delivery.carrier"].sudo().search([("delivery_type", "=", 'mfb')], limit=1)
        login = mfb_carrier.mfb_api_login
        password = mfb_carrier.mfb_api_password
        # password = "f3kVr4UsFtmVAxxKx1szBXRDbyFcGSsq"  # password for test API server

        req = requests.get(url_orders + order_id + "/tracking", auth=(login, password))
        res = json.loads(req.text)
        _logger.error(res)
        if res["data"]:
            context = {
                "status": res["data"][0]["events"][0]["details"]["label"]["en"],
                'happened_at': res["data"][0]["events"][0]["happened_at"],
            }
            status = "Current status: '%s', \n happened at: %s" % (context["status"], context["happened_at"])
            return status
        else:
            _logger.error("There is no data in response to get status")
            raise UserError(_('Status is currently unavailable. Please try again later'))

    @api.multi
    def get_delivery_status(self):
        order_id = self.delivery_order_uuid
        if not order_id:
            raise UserError(_('No order exist'))
        status = self.request_to_track_parcel(order_id)
        self.current_delivery_status = status
        raise UserError(status)

    @api.multi
    def get_return_status(self):
        order_id = self.return_order_uuid
        if not order_id:
            raise UserError(_('No order exist'))
        status = self.request_to_track_parcel(order_id)
        self.current_return_status = status
        raise UserError(status)

    @api.multi
    def request_to_cancel_order(self, order_id):
        url_orders = "https://api.myflyingbox.com/v2/orders/"
        # url_orders = "https://test.myflyingbox.com/v2/orders/"
        _logger.error(order_id)
        mfb_carrier = self.env["delivery.carrier"].sudo().search([("delivery_type", "=", 'mfb')], limit=1)
        login = mfb_carrier.mfb_api_login
        password = mfb_carrier.mfb_api_password
        # password = "f3kVr4UsFtmVAxxKx1szBXRDbyFcGSsq"  # password for test API server
        req = requests.put(url_orders + order_id + "/cancel", auth=(login, password))
        res = {}
        try:
            res = json.loads(req.text)
        except Exception as e:
            _logger.error(e.message)
        _logger.error(res)

        if res["status"] == 'success':
            return True
        else:
            _logger.error("Error: Order was not canceled")
            return False

    @api.multi
    def cancel_delivery_order(self):
        order_id = self.delivery_order_uuid
        if not order_id:
            raise UserError(_('No order exist'))
        if self.request_to_cancel_order(order_id):
            self.delivery_order_booked = False

    @api.multi
    def cancel_return_order(self):
        order_id = self.return_order_uuid
        if not order_id:
            raise UserError(_('No order exist'))
        if self.request_to_cancel_order(order_id):
            self.return_order_booked = False
