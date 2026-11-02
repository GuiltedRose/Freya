"""Strict decoding of pinned Pump events carried by authenticated self-CPI.

No wallet-delta inference: routed transactions can contain several independent
swaps. Unknown layouts remain quarantined rather than becoming volume.
"""
import base64
import hashlib
import json
from pathlib import Path
from solders.pubkey import Pubkey
from wallet_watch import decode58, SOL
from onchain_routes import route_binding, event_binding, validate_route

EVENT_IX = bytes.fromhex('e445a52e51cb9a1d')
ROOT = Path(__file__).parent / 'vendor'

# Explicit legacy wire variants, not defaults for arbitrary missing Borsh fields.
# Older official IDLs omit these trailing OptionBool arguments. Retained successful
# receipts also carry those shorter forms. None records absence, never false or a
# claim about the program's execution defaults. All supplied bool bytes stay strict.
EOF_FLAGS = {
    'pump': {
        'buy': {'track_volume', 'partial_fill'},
        'buy_exact_sol_in': {'track_volume', 'partial_fill'},
        'buy_v2': {'partial_fill'}, 'buy_exact_quote_in_v2': {'partial_fill'},
        'buy_v3': {'partial_fill'}, 'buy_exact_quote_in_v3': {'partial_fill'},
    },
    'pump_amm': {'buy': {'track_volume'}, 'buy_exact_quote_in': {'track_volume'}},
}


def read_value(raw, kind, offset, types):
    def take(size):
        if size < 0 or offset + size > len(raw):
            raise ValueError('Truncated Borsh field')
        return raw[offset:offset + size], offset + size
    if kind == 'pubkey':
        value, end = take(32)
        return str(Pubkey.from_bytes(value)), end
    if kind == 'bool':
        value, end = take(1)
        if value[0] not in (0, 1):
            raise ValueError('Invalid Borsh bool')
        return bool(value[0]), end
    if kind == 'string':
        size, offset = read_value(raw, 'u32', offset, types)
        if size > 16384:
            raise ValueError('Oversized string')
        value, end = take(size)
        return value.decode('utf8'), end
    if kind == 'bytes':
        size, offset = read_value(raw, 'u32', offset, types)
        if size > 16384:
            raise ValueError('Oversized bytes')
        value, end = take(size)
        return list(value), end
    if isinstance(kind, str) and kind[:1] in ('u', 'i') and kind[1:].isdigit():
        value, end = take(int(kind[1:]) // 8)
        return int.from_bytes(value, 'little', signed=kind[0] == 'i'), end
    if isinstance(kind, dict) and 'option' in kind:
        tag, offset = read_value(raw, 'u8', offset, types)
        if tag not in (0, 1):
            raise ValueError('Invalid Borsh option tag')
        return read_value(raw, kind['option'], offset, types) if tag else (None, offset)
    if isinstance(kind, dict) and 'defined' in kind:
        definition = types[kind['defined']['name']]
        if definition.get('kind') == 'enum':
            tag, offset = read_value(raw, 'u8', offset, types)
            if tag >= len(definition['variants']):
                raise ValueError('Invalid Borsh enum tag')
            variant = definition['variants'][tag]
            if variant.get('fields'):
                raise ValueError('Unreviewed enum payload')
            return variant['name'], offset
        fields = definition['fields']
        if fields and isinstance(fields[0], str):
            values = []
            for field in fields:
                value, offset = read_value(raw, field, offset, types)
                values.append(value)
            return values, offset
        return read_struct(raw, fields, offset, types)
    if isinstance(kind, dict) and ('vec' in kind or 'array' in kind):
        if 'vec' in kind:
            element = kind['vec']
            size, offset = read_value(raw, 'u32', offset, types)
        else:
            element, size = kind['array']
        if size > 4096:
            raise ValueError('Oversized vector')
        values = []
        for _ in range(size):
            value, offset = read_value(raw, element, offset, types)
            values.append(value)
        return values, offset
    raise ValueError('Unsupported Borsh type')


def read_struct(raw, fields, offset, types):
    values = {}
    for field in fields:
        values[field['name']], offset = read_value(raw, field['type'], offset, types)
    return values, offset


class Decoder:
    def __init__(self, backend='python', schema_directory=None):
        if backend not in ('python','rust','compare'): raise ValueError('Unknown event backend')
        self.backend=backend
        self.native_schemas={}
        self.specs = {}
        root = Path(schema_directory) if schema_directory else ROOT
        for name in ('pump', 'pump_amm'):
            raw = (root / (name + '.json')).read_bytes()
            manifest = json.loads((root / (name + '.manifest.json')).read_text())
            pinned = json.loads((ROOT / (name + '.manifest.json')).read_text())
            if manifest['program'] != pinned['program']:
                raise ValueError('Schema bundle cannot change program identity')
            if schema_directory and (manifest.get('reviewed') is not True or not manifest.get('evidence')):
                raise ValueError('External schema requires explicit review evidence')
            if hashlib.sha256(raw).hexdigest() != manifest['sha256']:
                raise ValueError('Pinned IDL hash mismatch')
            idl = json.loads(raw)
            if idl['address'] != manifest['program']:
                raise ValueError('Pinned program identity mismatch')
            self.specs[idl['address']] = {
                'hash': manifest['sha256'], 'venue': name,
                'types': {t['name']: t['type'] for t in idl['types']},
                'events': {bytes(e['discriminator']): e['name'] for e in idl['events']},
                'instructions': {bytes(i['discriminator']): i for i in idl['instructions']}}

    def event_fields(self, program, event_name, raw):
        spec=self.specs[program]
        fields=spec['types'][event_name]['fields']
        if self.backend=='python': return read_struct(raw,fields,16,spec['types'])
        key=(program,event_name)
        if key not in self.native_schemas:
            try: from _solana_binary import NativeBorshSchema
            except ImportError: raise ValueError('Native event schema unavailable; explicit backend cannot fall back') from None
            self.native_schemas[key]=NativeBorshSchema(json.dumps(fields),json.dumps(spec['types']))
        text,end=self.native_schemas[key].decode(raw,16)
        def restore(value):
            if isinstance(value,dict):
                if set(value)=={'$integer'}: return int(value['$integer'])
                return {key:restore(item) for key,item in value.items()}
            if isinstance(value,list): return [restore(item) for item in value]
            return value
        native=(restore(json.loads(text)),end)
        if self.backend=='compare' and native!=read_struct(raw,fields,16,spec['types']):
            raise ValueError('Native event field parity mismatch')
        return native

    def transaction(self, tx, slot, tx_index, block_time):
        meta, transaction = tx['meta'], tx['transaction']
        if meta is None:
            return [], ['missing transaction metadata']
        if meta['err'] is not None:
            return [], []
        keys = transaction['message']['accountKeys']
        keys = [key['pubkey'] if isinstance(key, dict) else key for key in keys]
        # json encoding needs loaded addresses; jsonParsed already expands keys.
        if not any(isinstance(k, dict) for k in transaction['message']['accountKeys']):
            loaded = meta.get('loadedAddresses') or {}
            keys += loaded.get('writable', []) + loaded.get('readonly', [])
        decimals = {}
        for balance in (meta.get('preTokenBalances') or []) + (meta.get('postTokenBalances') or []):
            mint, digits = balance['mint'], balance['uiTokenAmount']['decimals']
            if type(digits) is not int or not 0 <= digits <= 18 or mint in decimals and decimals[mint] != digits:
                return [], ['ambiguous mint decimals']
            decimals[mint] = digits
        signature = transaction['signatures'][0]
        inner = {group['index']: group['instructions'] for group in meta.get('innerInstructions') or []}
        events, issues = [], []
        for outer, root in enumerate(transaction['message']['instructions']):
            parents = {}
            swaps = []
            emitted = set()
            supplements = set()
            routes = {}
            for ordinal, ix in enumerate([root] + inner.get(outer, [])):
                try:
                    program = ix.get('programId') or keys[ix['programIdIndex']]
                    height = 1 if ordinal == 0 else ix.get('stackHeight')
                    if height is None:
                        if program in self.specs:
                            issues.append('missing CPI stack height')
                        continue
                    for level in list(parents):
                        if level >= height:
                            del parents[level]
                    if program not in self.specs:
                        continue
                    spec = self.specs[program]
                    raw = decode58(ix.get('data', ''))
                    accounts = [keys[a] if isinstance(a, int) else a for a in ix.get('accounts', [])]
                    if raw[:8] != EVENT_IX:
                        instruction = spec['instructions'].get(raw[:8])
                        if instruction is None:
                            issues.append('unknown program instruction: ' + program)
                            continue
                        names = [a['name'] for a in instruction['accounts']]
                        binding = dict(zip(names, accounts))
                        binding = route_binding(program,instruction['name'],binding,accounts,len(names),parents,ordinal,
                                                {value['venue']:key for key,value in self.specs.items()})
                        if instruction['name'] == 'multi_hop_swap':
                            routes[ordinal] = binding['_route_hops']
                        parents[height] = (program, instruction['name'], binding, ordinal, None)
                        if instruction['name'].startswith(('buy', 'sell')) or (
                            spec['venue'] == 'pump_amm' and instruction['name'] == 'boost_buy_and_burn'):
                            swaps.append(ordinal)
                        arguments, end = {}, 8
                        for argument in instruction['args']:
                            optional=EOF_FLAGS[spec['venue']].get(instruction['name'],set())
                            optional_type='OptionBool' if argument['name'] in optional else None
                            # The pinned SDK's create_v2 docs explicitly mark these
                            # trailing fields EOF-tolerant. Record absence, not defaults.
                            if spec['venue']=='pump' and instruction['name']=='create_v2':
                                optional_type={'creator_fee_bps':'OptionU64','is_holder_reward':'OptionBool'}.get(argument['name'])
                            if (end == len(raw) and optional_type is not None
                                and argument['type'] == {'defined': {'name': optional_type}}):
                                arguments[argument['name']] = None
                            else:
                                arguments[argument['name']], end = read_value(raw, argument['type'], end, spec['types'])
                        if end != len(raw):
                            raise ValueError('Unreviewed instruction layout/version')
                        parents[height] = (program, instruction['name'], binding, ordinal, arguments)
                        continue
                    authority = str(Pubkey.find_program_address([b'__event_authority'], Pubkey.from_string(program))[0])
                    if not accounts or accounts[0] != authority:
                        raise ValueError('Event authority mismatch')
                    parent = parents.get(height - 1)
                    if not parent or parent[0] != program:
                        raise ValueError('Unbound self-CPI event')
                    event_name = spec['events'].get(raw[8:16])
                    if event_name is None:
                        raise ValueError('Unknown event discriminator')
                    fields, end = self.event_fields(program,event_name,raw)
                    if end != len(raw):
                        issues.append(f'{event_name}: payload {len(raw)-16} bytes, pinned fields {end-16}, trailing {len(raw)-end}; program {program}')
                        raise ValueError('Unreviewed event layout/version')
                    event = self.normalize(spec, event_name, fields, parent, decimals)
                    event.update(instruction_name=parent[1], instruction_args=parent[4],
                                 omitted_instruction_args=[key for key,value in (parent[4] or {}).items() if value is None],
                                 parent_instruction_index=parent[3],
                                 signature=signature, slot=slot, tx_index=tx_index,
                                 outer_index=outer, event_index=ordinal, block_time=block_time,
                                 program=program, idl_hash=spec['hash'], event_name=event_name,
                                 transaction_fee_lamports=meta['fee'], raw_event=base64.b64encode(raw).decode())
                    events.append(event)
                    event_key=((event['route_parent_instruction_index'],event['route_hop_index'])
                               if 'route_hop_index' in event else parent[3])
                    if event['kind'] == 'swap' and event_name == 'PostCompleteBuyEvent':
                        if event_key in supplements:
                            raise ValueError('Duplicate post-completion event')
                        supplements.add(event_key)
                    elif event['kind'] == 'swap':
                        if event_key in emitted:
                            raise ValueError('Multiple trade events for one instruction')
                        emitted.add(event_key)
                except (KeyError, ValueError, IndexError, TypeError, UnicodeError) as error:
                    issues.append(str(error))
            if not supplements.issubset(emitted):
                issues.append('post-completion buy without primary trade event')
            if set(swaps) != {key for key in emitted if isinstance(key,int)}:
                issues.append('trade instruction/event mismatch')
            for route_id,hops in routes.items():
                try:
                    validate_route(hops,[event for event in events if event['outer_index']==outer],route_id)
                except ValueError as error:
                    issues.append(str(error))
            boost_groups={}
            for event in events:
                if event['outer_index'] == outer and event['instruction_name'] == 'boost_buy_and_burn':
                    boost_groups.setdefault(event['parent_instruction_index'],[]).append(event)
            for boost in boost_groups.values():
                # The pool trade and burn receipt describe the same protocol action.
                buys=[event for event in boost if event['event_name'] == 'BuyEvent']
                burns=[event for event in boost if event['event_name'] == 'BoostBuyAndBurnEvent']
                if len(buys) != 1 or len(burns) != 1:
                    issues.append('Missing or duplicate protocol buy/burn receipt')
                else:
                    buy,burn=buys[0],burns[0]
                    f,g=buy['fields'],burn['fields']
                    if (buy['pool'] != burn['pool'] or buy['mint'] != burn['mint']
                        or f['timestamp'] != g['timestamp']
                        or int(buy['base_units']) != g['base_amount_burned']
                        or f['user_quote_amount_in'] != g['quote_amount_in_used']
                        or f['pool_base_token_reserves']-int(buy['base_units']) != g['base_reserves_after']
                        or f['pool_quote_token_reserves']+f['quote_amount_in_with_lp_fee'] != g['real_quote_reserves_after']):
                        issues.append('Protocol buy/burn accounting mismatch')
        if events or issues:
            transfers, transfer_issues = self.transfers(tx, keys)
            issues.extend(transfer_issues)
            for event in events:
                event['transaction_token_transfers'] = transfers
        # Transfers are transaction-scoped, never allocated by net wallet delta.
        return events, sorted(set(issues))

    def transfers(self, tx, keys):
        """Decode standard SPL transfers, retaining explicit unresolved extensions."""
        balances = {}
        for row in (tx['meta'].get('preTokenBalances') or []) + (tx['meta'].get('postTokenBalances') or []):
            balances[row['accountIndex']] = row
        result, issues = [], []
        groups = [(i, [ix]) for i, ix in enumerate(tx['transaction']['message']['instructions'])]
        groups += [(g['index'], g['instructions']) for g in tx['meta'].get('innerInstructions') or []]
        token_programs = {'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                          'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}
        for outer, instructions in groups:
            for index, ix in enumerate(instructions):
                program = ix.get('programId') or keys[ix['programIdIndex']]
                if program not in token_programs or 'data' not in ix:
                    continue
                try:
                    raw = decode58(ix['data'])
                    fee_transfer=(program == 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
                                  and raw[:2] == b'\x1a\x01')
                    if raw[0] not in (3, 12) and not fee_transfer:
                        if raw[0] == 26:
                            issues.append('Token-2022 transfer fee extension requires reviewed decoder')
                        continue
                    checked = raw[0] == 12 or fee_transfer
                    if len(raw) != (19 if fee_transfer else 10 if checked else 9):
                        raise ValueError('Unreviewed SPL transfer layout')
                    accounts = [keys[a] if isinstance(a, int) else a for a in ix['accounts']]
                    source, destination = accounts[0], accounts[2 if checked else 1]
                    matching = [row for account_index, row in balances.items()
                                if keys[account_index] in (source, destination)]
                    mints = {row['mint'] for row in matching}
                    digits = {row['uiTokenAmount']['decimals'] for row in matching}
                    if checked:
                        # TransferChecked supplies the mint and decimals explicitly;
                        # the successful token-program invocation checks both. Closed
                        # temporary accounts need not appear in pre/post balances.
                        mint,decimals=accounts[1],raw[10 if fee_transfer else 9]
                        if not 0 <= decimals <= 18:
                            raise ValueError('Unsupported SPL checked transfer decimals')
                        if matching and (mints != {mint} or digits != {decimals}):
                            raise ValueError('SPL checked transfer identity mismatch')
                    else:
                        if len(mints) != 1 or len(digits) != 1:
                            raise ValueError('SPL transfer identity unavailable')
                        mint, decimals = next(iter(mints)), next(iter(digits))
                    amount=int.from_bytes(raw[2:10] if fee_transfer else raw[1:9],'little')
                    fee=int.from_bytes(raw[11:19],'little') if fee_transfer else 0
                    if fee > amount:
                        raise ValueError('SPL transfer fee exceeds amount')
                    fee_known=fee_transfer or program == 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
                    result.append({'outer_index': outer, 'instruction_index': index,
                                   'program': program, 'mint': mint, 'source': source,
                                   'destination': destination, 'decimals': decimals,
                                   'self_transfer':source == destination,
                                   'identity_basis':'checked_instruction' if checked else 'balance_metadata',
                                   'amount_units':str(amount),'expected_fee_units':str(fee) if fee_known else None,
                                   'debited_units':str(0 if source == destination else amount),
                                   'credited_units':'0' if source == destination else str(amount-fee) if fee_known else None})
                except (ValueError, KeyError, IndexError, TypeError) as error:
                    issues.append(str(error))
        return result, issues

    def normalize(self, spec, name, f, parent, decimals):
        _, instruction, binding, _, _ = parent
        binding, route = event_binding(spec,name,f,binding)
        result = {'kind': 'lifecycle', 'fields': f, 'venue': spec['venue'], **route}
        if name not in ('TradeEvent', 'BuyEvent', 'SellEvent', 'PostCompleteBuyEvent'):
            mint = f.get('mint', f.get('base_mint'))
            bound_mint = binding.get('mint', binding.get('base_mint'))
            if mint and bound_mint and mint != bound_mint:
                raise ValueError('Lifecycle mint identity mismatch')
            if name == 'BoostBuyAndBurnEvent' and (
                instruction != 'boost_buy_and_burn' or f['pool'] != binding.get('pool')
                or f['authority'] != binding.get('authority')):
                raise ValueError('Protocol burn identity mismatch')
            result.update(mint=mint, pool=f.get('pool', f.get('bonding_curve')),
                          wallet=f.get('user', f.get('creator')))
            return result
        buy = f['is_buy'] if name == 'TradeEvent' else name in ('BuyEvent', 'PostCompleteBuyEvent')
        protocol_buy=(spec['venue'] == 'pump_amm' and instruction == 'boost_buy_and_burn'
                      and name == 'BuyEvent')
        if not route and not protocol_buy and not instruction.startswith('buy' if buy else 'sell'):
            raise ValueError('Trade direction/instruction mismatch')
        mint = binding.get('mint', binding.get('base_mint'))
        quote = binding.get('quote_mint', SOL)
        pool = binding.get('pool', binding.get('bonding_curve'))
        user = binding.get('boost_vault_authority') if protocol_buy else binding.get('user')
        if protocol_buy:
            authority=str(Pubkey.find_program_address(
                [b'boost_vault',bytes(Pubkey.from_string(pool))],Pubkey.from_string(parent[0]))[0])
            if user != authority or f['user_quote_token_account'] != binding.get('boost_vault'):
                raise ValueError('Protocol boost vault identity mismatch')
        if mint is None or pool is None or user is None or mint == quote:
            raise ValueError('Missing trade identity')
        if f.get('mint', mint) != mint or f.get('pool', pool) != pool or f['user'] != user:
            raise ValueError('Trade event identity mismatch')
        if name in ('TradeEvent', 'PostCompleteBuyEvent'):
            event_quote = f['quote_mint']
            # Legacy native-SOL events encode the default pubkey as quote mint.
            if event_quote == str(Pubkey.default()):
                event_quote = SOL
            if event_quote != quote:
                raise ValueError('Quote identity mismatch')
            if name == 'PostCompleteBuyEvent':
                if (not route and instruction not in ('buy_v3', 'buy_exact_quote_in_v3')) or f['bonding_curve'] != pool:
                    raise ValueError('Unbound post-completion trade')
                amount, quote_amount = f['base_out'], f['quote_in']
            else:
                amount = f['token_amount']
                quote_amount = f['sol_amount'] if quote == SOL else f['quote_amount']
        else:
            amount = f['base_amount_out' if buy else 'base_amount_in']
            quote_amount = f['quote_amount_in' if buy else 'quote_amount_out']
        if mint not in decimals or quote != SOL and quote not in decimals:
            raise ValueError('Mint decimals unavailable from transaction')
        if min(amount, quote_amount) <= 0:
            raise ValueError('Nonpositive swap amounts')
        if quote == SOL and f.get('quote_transfer_fee', 0) != 0:
            raise ValueError('Native SOL cannot charge a Token-2022 quote transfer fee')
        fees = {k: f[k] for k in ('fee', 'creator_fee', 'lp_fee', 'protocol_fee',
                                 'coin_creator_fee', 'cashback', 'buyback_fee', 'holder_rewards',
                                 'quote_transfer_fee') if k in f}
        result.update(kind='swap', mint=mint, quote_mint=quote, pool=pool, wallet=user,
                      direction='buy' if buy else 'sell', base_units=str(amount),
                      quote_units=str(quote_amount), base_decimals=decimals[mint],
                      quote_decimals=9 if quote == SOL else decimals[quote], fees=fees,
                      reserves={k: v for k, v in f.items() if 'reserves' in k},
                      amount_basis='program trade event; fees separate; not net wallet delta')
        if protocol_buy:
            result.update(wallet=None,protocol_actor=user,actor_kind='protocol_buy_and_burn')
        if route:
            result['route_user']=user
            wallet_leg=(route['route_hop_index'] == route['route_hop_count']-1 if buy
                        else route['route_hop_index'] == 0)
            if not wallet_leg:
                result.update(wallet=None,actor_kind='routed_intermediate')
        return result
