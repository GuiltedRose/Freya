// Appended to pinned ledger/src/shred.rs; use Agave's Merkle/RS implementation.
pub fn freya_recover(shreds: Vec<Shred>, cache: &ReedSolomonCache) -> Result<Vec<Shred>, Error> {
    let merkle = shreds.into_iter().map(merkle::Shred::try_from).collect::<Result<Vec<_>, _>>()?;
    merkle::recover(merkle, cache)?.map(|s|s.map(Shred::from)).collect()
}
