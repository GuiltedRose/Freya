// Appended to pinned serve_repair.rs: expose existing authenticated ping handling.
pub fn freya_repair_pong(data: &[u8], keypair: &Keypair, expected: &Pubkey) -> Option<Vec<u8>> {
    let RepairResponse::Ping(ping) = wincode::deserialize(data).ok()?;
    if !ping.verify() || ping.pubkey() != *expected { return None; }
    wincode::serialize(&RepairProtocol::Pong(Pong::new(&ping, keypair))).ok()
}
