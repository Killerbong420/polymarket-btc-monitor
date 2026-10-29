import unittest
from exchange_prices import parse_response, summarize_prices, public_snapshot

class ExchangePricesTest(unittest.TestCase):
    def test_vendor_formats(self):
        self.assertEqual(parse_response('Coinbase',{'price':'80500','time':'2026-10-29T00:00:00Z'})[0],80500)
        self.assertEqual(parse_response('Kraken',{'error':[],'result':{'XXBTZUSD':{'c':['80501','1']}}})[0],80501)
        self.assertEqual(parse_response('Bitstamp',{'last':'80502','timestamp':'1793232000'})[0],80502)
        self.assertEqual(parse_response('Gemini',{'last':'80503'})[0],80503)
        self.assertEqual(parse_response('Bitfinex',[0,0,0,0,0,0,80504])[0],80504)
        self.assertEqual(parse_response('Binance.US',{'price':'80505'})[0],80505)
        self.assertEqual(parse_response('Binance Global',{'price':'80506'})[0],80506)
        self.assertEqual(parse_response('OKX',{'data':[{'last':'80507','ts':'1793232000000'}]})[0],80507)

    def test_price_comparison_separates_usdt_and_rejects_outlier(self):
        now=10000
        prices=[('Coinbase','USD',80500),('Kraken','USD',80502),
                ('Bitstamp','USD',80506),('Gemini','USD',80508),
                ('Bitfinex','USD',90000),('Binance.US','USD',80501),
                ('Binance Global','USDT',80900),('OKX','USDT',80910)]
        rows=[{'source':name,'quote':quote,'status':'responding','price':price,'fetched_epoch':now-2} for name,quote,price in prices]
        result=summarize_prices(rows,now)
        self.assertEqual(result['usd']['source_count'],5)
        self.assertEqual(result['usd']['status'],'available')
        self.assertEqual(result['usdt']['source_count'],2)
        self.assertNotEqual(result['usd']['median'],result['usdt']['median'])
        self.assertEqual(result['exchanges'][4]['status'],'outlier')
        self.assertLess(result['usd']['spread_usd'],10)

    def test_missing_and_stale_data_not_valid_consensus(self):
        rows=[{'source':'Coinbase','quote':'USD','status':'responding','price':81000,'fetched_epoch':5},
              {'source':'Kraken','quote':'USD','status':'responding','price':81100,'fetched_epoch':95},
              {'source':'Gemini','quote':'USD','status':'unavailable','reason':'HTTPStatusError'}]
        result=summarize_prices(rows,now=100)
        self.assertEqual(result['usd']['source_count'],1)
        self.assertEqual(result['usd']['status'],'limited')
        self.assertEqual(result['exchanges'][0]['status'],'stale')
        self.assertNotIn('price',result['exchanges'][0])
        hidden=public_snapshot(result,now=200)
        self.assertEqual(hidden['status'],'stale')
        self.assertNotIn('price',hidden['exchanges'][1])
        self.assertEqual(hidden['usd']['source_count'],0)

if __name__=='__main__':unittest.main()
