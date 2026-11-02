"""Strict observed multi-hop bindings, never execution or wallet-delta inference.

Contract: official MULTI_HOP_SWAP.md and pump-sdk 4.3.1 hop-account builder.
Every venue must be a canonical pool or derived bonding curve in the instruction.
"""
from solders.pubkey import Pubkey


def pda(program, *seeds):
    return str(Pubkey.find_program_address(list(seeds), Pubkey.from_string(program))[0])


def route_binding(program, instruction, binding, accounts, fixed, parents, ordinal, programs):
    if instruction not in ('multi_hop_swap', 'multi_hop_curve_swap'):
        return binding
    pump, amm = programs['pump'], programs['pump_amm']
    expected_program = amm if instruction == 'multi_hop_swap' else pump
    if program != expected_program:
        raise ValueError('Unexpected route program')
    remaining = accounts[fixed:]
    if not remaining or len(remaining) % 5:
        raise ValueError('Incomplete five-account route hop')
    hops = []
    for offset in range(0, len(remaining), 5):
        base, quote, venue, base_vault, quote_vault = remaining[offset:offset+5]
        if base == quote:
            raise ValueError('Route hop has identical mints')
        base_bytes = bytes(Pubkey.from_string(base))
        curve = pda(pump, b'bonding-curve', base_bytes)
        authority = pda(pump, b'pool-authority', base_bytes)
        pool = pda(amm, b'pool', bytes(2), bytes(Pubkey.from_string(authority)),
                   base_bytes, bytes(Pubkey.from_string(quote)))
        if venue not in (curve, pool):
            raise ValueError('Route venue is not the canonical pool or curve')
        hops.append({'mint': base, 'quote_mint': quote, 'pool': venue,
                     'venue': 'pump' if venue == curve else 'pump_amm',
                     'base_vault': base_vault, 'quote_vault': quote_vault})
    binding = dict(binding)
    if instruction == 'multi_hop_swap':
        binding['_route_id'] = ordinal
        binding['_route_hop_count'] = len(hops)
        binding['_route_hops'] = [dict(hop, index=index) for index, hop in enumerate(hops)]
        return binding
    ancestors = [value for _, value in sorted(parents.items(), reverse=True)
                 if value[0] == amm and value[1] == 'multi_hop_swap']
    if not ancestors:
        raise ValueError('Curve route lacks authenticated PumpSwap parent')
    root = ancestors[0][2]
    if (binding.get('user') != root['user']
        or binding.get('global') != root.get('pump_global')
        or binding.get('pump_amm_global_config') != root.get('global_config')):
        raise ValueError('Curve route parent identity mismatch')
    selected = []
    for hop in hops:
        matches = [candidate for candidate in root['_route_hops']
                   if all(candidate[key] == value for key, value in hop.items())]
        if len(matches) != 1 or hop['venue'] != 'pump':
            raise ValueError('Curve hop is not uniquely bound to parent route')
        selected.append(matches[0])
    indices = [hop['index'] for hop in selected]
    if indices != list(range(indices[0], indices[0]+len(indices))):
        raise ValueError('Curve route hop order mismatch')
    binding.update(_route_id=root['_route_id'], _route_hops=selected,
                   _route_hop_count=root['_route_hop_count'])
    return binding


def event_binding(spec, name, fields, binding):
    """Select one authenticated hop without changing the recorded instruction."""
    if '_route_hops' not in binding:
        return binding, {}
    if name not in ('TradeEvent', 'BuyEvent', 'SellEvent', 'PostCompleteBuyEvent'):
        return binding, {}
    if name in ('TradeEvent', 'BuyEvent') and fields.get('ix_name') != 'multi_hop_swap':
        raise ValueError('Route event instruction identity mismatch')
    matches = [hop for hop in binding['_route_hops'] if hop['venue'] == spec['venue']
               and (fields.get('mint', hop['mint']) == hop['mint'])
               and (fields.get('pool', hop['pool']) == hop['pool'])
               and (fields.get('bonding_curve', hop['pool']) == hop['pool'])]
    if len(matches) != 1:
        raise ValueError('Route event does not identify one hop')
    hop = matches[0]
    bound = dict(binding, mint=hop['mint'], base_mint=hop['mint'], quote_mint=hop['quote_mint'],
                 pool=hop['pool'], bonding_curve=hop['pool'])
    return bound, {'route_parent_instruction_index': binding['_route_id'],
                   'route_hop_index': hop['index'],'route_hop_count':binding['_route_hop_count']}


def validate_route(hops, events, route_id):
    primary = [event for event in events if event.get('route_parent_instruction_index') == route_id
               and event['kind'] == 'swap' and event['event_name'] != 'PostCompleteBuyEvent']
    if sorted(event['route_hop_index'] for event in primary) != list(range(len(hops))):
        raise ValueError('Missing or duplicate route trade event')
    ordered = sorted(primary, key=lambda event: event['route_hop_index'])
    if len({event['direction'] for event in ordered}) != 1:
        raise ValueError('Mixed route directions')
    buy = ordered[0]['direction'] == 'buy'
    for left, right in zip(hops, hops[1:]):
        if (left['mint'] != right['quote_mint'] if buy else left['quote_mint'] != right['mint']):
            raise ValueError('Discontinuous route mints')
