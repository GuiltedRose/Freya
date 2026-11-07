// Appended to pinned serve_repair.rs: reuse authenticated repair ping handling.
pub fn freya_repair_pong_checked(data: &[u8], keypair: &Keypair, expected: &Pubkey)
    -> Result<Vec<u8>, &'static str> {
    if data.len()!=132 || data[..4]!=[0;4] {return Err("ping_framing");}
    let RepairResponse::Ping(ping) = wincode::deserialize(data).map_err(|_|"ping_decode_failed")?;
    if !ping.verify() {return Err("ping_signature_invalid");}
    if ping.pubkey()!=*expected {return Err("ping_identity_mismatch");}
    wincode::serialize(&RepairProtocol::Pong(Pong::new(&ping,keypair))).map_err(|_|"pong_encode_failed")
}
pub fn freya_repair_pong(data: &[u8], keypair: &Keypair, expected: &Pubkey) -> Option<Vec<u8>> {
    freya_repair_pong_checked(data,keypair,expected).ok()
}
