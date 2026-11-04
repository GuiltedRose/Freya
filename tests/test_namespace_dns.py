import unittest
from solana_network_lifecycle import upstream_resolvers
from prepare_native_window import planning_error


class DnsTests(unittest.TestCase):
    def test_stub_and_unusable_dns_are_never_copied(self):
        text='\n'.join('nameserver '+value for value in
                       ('127.0.0.53','::1','0.0.0.0','224.0.0.1','169.254.203.1','bad'))
        self.assertEqual(upstream_resolvers(text),[])

    def test_actual_host_router_and_upstreams_are_preserved_not_invented(self):
        text='nameserver 10.0.0.1\nnameserver 10.0.0.1\nnameserver 1.1.1.1 # host setting\n'
        self.assertEqual(upstream_resolvers(text),['10.0.0.1','1.1.1.1'])
        self.assertEqual(upstream_resolvers(''),[])

    def test_planner_errors_do_not_expose_exception_text_or_urls(self):
        error=ConnectionError('https://private.invalid/SECRET')
        error.category='connect_timeout';error.freya_rpc_method='getGenesisHash';error.socket_errno=110
        record=planning_error(error)
        self.assertEqual(record['rpc_method'],'getGenesisHash')
        self.assertEqual(record['category'],'connect_timeout')
        self.assertEqual(record['socket_errno'],110)
        self.assertNotIn('SECRET',str(record))
        error.category='SECRET';error.freya_rpc_method='SECRET'
        self.assertNotIn('SECRET',str(planning_error(error)))
