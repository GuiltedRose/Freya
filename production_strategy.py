"""Single production signal; numerical risk limits remain in DipRules."""
NAME = 'momentum_reclaim'


def validate(rules):
    if rules.entry_style != 'volume_candles' or rules.momentum_reclaim_enabled is not True:
        raise ValueError('Production requires the momentum reclaim strategy')
    return rules
