"""Offline fixtures only; no application authorization or network traffic."""
import unittest
from solders.keypair import Keypair
from inspect_repair_ping import correlate


class RepairPingInspectionTests(unittest.TestCase):
    def setUp(self):
        self.key = Keypair()
        token = bytes(range(32))
        self.payload = bytes(4) + bytes(self.key.pubkey()) + token + bytes(self.key.sign_message(token))
        self.source = '86.105.224.20:8008'
        row = dict(session_id='session', local_socket='0.0.0.0:34886', endpoint=self.source,
                   peer=str(self.key.pubkey()), sent_at=100.0, nonce=123, slot=465539000)
        self.checkpoint = dict(repair_session_id='session', repair_request_history=[row],
            last_repair=dict(session_id='session', local_socket=row['local_socket'],
                unexpected_sample=dict(source=self.source, bytes=132, received_at=108.0)))

    def test_valid_signature_correlates_prior_send_but_does_not_authorize(self):
        result = correlate(self.checkpoint, self.payload, self.source, '169.254.203.2:34886', 102.0)
        self.assertEqual(len(result['matching_sends']), 1)
        self.assertTrue(result['signature_valid'])
        self.assertEqual(result['capture_to_application_seconds'], 6.0)
        self.assertFalse(result['authorized_response'])
        self.assertIn('not_proven', result['sample_packet_identity'])

    def test_other_identity_socket_session_endpoint_or_future_send_does_not_correlate(self):
        row = self.checkpoint['repair_request_history'][0]
        for key, value in [('peer', str(Keypair().pubkey())), ('session_id', 'old'),
                           ('endpoint', '1.1.1.1:8008'), ('local_socket', '0.0.0.0:1234'),
                           ('sent_at', 103.0)]:
            with self.subTest(key=key):
                prior = row[key]
                row[key] = value
                self.assertEqual(correlate(self.checkpoint, self.payload, self.source, '169.254.203.2:34886', 102.0)['matching_sends'], [])
                row[key] = prior

    def test_wrong_capture_destination_port_does_not_correlate(self):
        result = correlate(self.checkpoint, self.payload, self.source, '169.254.203.2:1234', 102.0)
        self.assertFalse(result['destination_port_matches_socket'])
        self.assertEqual(result['matching_sends'], [])

    def test_forged_signature_and_gossip_frame_are_not_accepted(self):
        forged = bytearray(self.payload)
        forged[68] ^= 1
        result = correlate(self.checkpoint, bytes(forged), self.source, '169.254.203.2:34886', 102.0)
        self.assertFalse(result['signature_valid'])
        self.assertEqual(result['matching_sends'], [])
        with self.assertRaises(ValueError):
            correlate(self.checkpoint, bytes([4]) + self.payload[1:], self.source, '169.254.203.2:34886', 102.0)
