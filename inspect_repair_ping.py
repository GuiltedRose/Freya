"""Offline pinned-Agave repair-ping verification and checkpoint correlation."""
import argparse
import ipaddress
import json
import math
from pathlib import Path


def endpoint(value):
    host, port = value.rsplit(':', 1)
    number = int(port)
    if not 1 <= number <= 65535:
        raise ValueError('Invalid UDP port')
    return str(ipaddress.IPv4Address(host)), number


def correlate(checkpoint, payload, source, destination, capture_at):
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    if not math.isfinite(capture_at):
        raise ValueError('Finite capture epoch required')
    endpoint(source)
    _, destination_port = endpoint(destination)
    if len(payload) != 132 or payload[:4] != bytes(4):
        raise ValueError('Not the pinned 132-byte tag-zero repair ping layout')
    signer = Pubkey.from_bytes(payload[4:36])
    valid = Signature.from_bytes(payload[68:132]).verify(signer, payload[36:68])
    repair = checkpoint.get('last_repair', {})
    sample = repair.get('unexpected_sample', {})
    session = checkpoint.get('repair_session_id')
    local_socket = repair.get('local_socket')
    port_matches = bool(local_socket) and endpoint(local_socket)[1] == destination_port
    history = checkpoint.get('repair_request_history', [])
    if not isinstance(history, list) or len(history) > 16:
        raise ValueError('Expected bounded request history')
    received_at = sample.get('received_at')
    received_at = (received_at if type(received_at) in (int, float)
                   and math.isfinite(received_at) else None)
    result = {
        'signature_valid': valid, 'signer': str(signer),
        'capture_epoch': capture_at, 'destination_port_matches_socket': port_matches,
        'sample_metadata_matches': sample.get('source') == source and sample.get('bytes') == len(payload),
        'capture_to_application_seconds': None if received_at is None else received_at - capture_at,
        'sample_packet_identity': 'not_proven_without_matching_payload_or_digest',
        'matching_sends': [],
        'authorized_response': False,
    }
    # A ping signs its token, not a request nonce. A matching historical send is
    # correlation evidence only; this program never answers or admits packets.
    if valid and port_matches and session and local_socket and repair.get('session_id') == session:
        for row in history:
            if not isinstance(row, dict):
                raise ValueError('Malformed request history entry')
            sent_at = row.get('sent_at')
            if (row.get('session_id') == session and row.get('local_socket') == local_socket
                    and row.get('endpoint') == source and row.get('peer') == str(signer)
                    and type(sent_at) in (int, float) and math.isfinite(sent_at)
                    and sent_at <= capture_at):
                result['matching_sends'].append({key: row.get(key) for key in
                    ('sent_at', 'peer', 'endpoint', 'local_socket', 'nonce', 'slot', 'handshake_followup')})
    result['interpretation'] = ('signed_ping_with_prior_selected_peer_send'
                                if result['matching_sends'] else
                                'signed_ping_without_prior_correlated_send' if valid else 'invalid_signature')
    return result


def read_bounded(path, limit):
    with path.open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError('Input exceeds offline evidence bound')
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--payload-hex', type=Path, required=True)
    parser.add_argument('--source', required=True, help='Captured source IPv4:port')
    parser.add_argument('--destination', required=True, help='Captured destination IPv4:port; account for NAT')
    parser.add_argument('--capture-epoch', type=float, required=True)
    args = parser.parse_args()
    try:
        checkpoint = json.loads(read_bounded(args.checkpoint, 1_048_576))
        if not isinstance(checkpoint, dict):
            raise ValueError('Expected checkpoint object')
        payload = bytes.fromhex(read_bounded(args.payload_hex, 4096).decode('ascii').replace(':', ''))
        result = correlate(checkpoint, payload, args.source, args.destination, args.capture_epoch)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, UnicodeError):
        parser.exit(2, 'Offline inspection failed: invalid, unavailable or malformed evidence\n')
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
