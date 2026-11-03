//! Bounded UDP shred acquisition. Native records are quarantined, not execution receipts.
use super::*;
use solana_entry::block_component::BlockComponent;
use solana_ledger::shred::{Shred, Shredder};
use solana_pubkey::Pubkey;
use std::{collections::BTreeMap, net::UdpSocket, str::FromStr};

pub(super) fn atomic_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    if let Some((root,limit)) = NATIVE_DISK_LIMIT.get() {
        if !path.starts_with(root) || super::follow::disk_bytes(root)?
            .saturating_add(bytes.len() as u64).saturating_add(65536) > *limit {
            return Err(io::Error::other("native disk bound; evidence retained"));
        }
    }
    let temporary = path.with_extension("pending");
    let mut file = OpenOptions::new().write(true).create(true).truncate(true)
        .mode(0o600).custom_flags(0x20000).open(&temporary)?;
    file.write_all(bytes)?; file.sync_all()?;
    fs::rename(&temporary,path)?;
    File::open(path.parent().ok_or_else(invalid)?)?.sync_all()
}
pub(super) fn raw(path: &Path) -> io::Result<Vec<u8>> {
    let mut file = OpenOptions::new().read(true).custom_flags(0x20000).open(path)?;
    if file.metadata()?.len() > 2048 { return Err(invalid()); }
    let mut bytes = Vec::new(); file.read_to_end(&mut bytes)?; Ok(bytes)
}
pub(super) fn checked(bytes: Vec<u8>, slot: u64, version: u16, leader: &Pubkey, max: u32) -> io::Result<Shred> {
    let shred = Shred::new_from_serialized_shred(bytes).map_err(|_| invalid())?;
    shred.sanitize().map_err(|_| invalid())?;
    if !shred.is_data() || shred.slot()!=slot || shred.version()!=version
        || shred.index()>=max || !shred.verify(leader) { return Err(invalid()); }
    Ok(shred)
}
pub(super) fn reconstruct(dir: &Path, slot: u64, version: u16, leader: &Pubkey, max: u32) -> io::Result<Value> {
    let mut shreds = BTreeMap::new();
    for row in fs::read_dir(dir)? {
        let path = row?.path();
        if path.extension().and_then(|s|s.to_str()) != Some("shred") { continue; }
        let shred = checked(raw(&path)?,slot,version,leader,max)?;
        if shreds.insert(shred.index(),shred).is_some() { return Err(invalid()); }
    }
    let last = shreds.values().filter(|s|s.last_in_slot()).map(|s|s.index()).collect::<Vec<_>>();
    if last.len()>1 { return Err(io::Error::other("conflicting terminal shreds")); }
    let mut missing = Vec::new();
    let end = last.first().copied().or_else(||shreds.keys().next_back().copied());
    if let Some(end) = end {
        for index in 0..=end { if !shreds.contains_key(&index) { missing.push(index); } }
        if shreds.keys().any(|i|*i>end) { return Err(invalid()); }
    }
    let complete = last.len()==1 && missing.is_empty();
    let mut result = json!({"schema":"freya-native-slot-v1","slot":slot,"leader":leader.to_string(),
        "shred_version":version,"received_shreds":shreds.len(),"missing_indices":missing,
        "terminal_index":last.first(),"reconstructed":false,"execution_metadata":null,
        "finality":"unverified","verification":"operator_configured_leader_signature",
        "authoritative":false,"conflict":dir.join("conflict.json").exists()});
    if !complete || result["conflict"]==true { return Ok(result); }
    let mut group = Vec::new();
    let mut transactions = Vec::new();
    let mut signatures = Vec::new();
    let mut parent = None;
    for shred in shreds.values() {
        let shred_parent = shred.parent().map_err(|_|invalid())?;
        if parent.is_some_and(|p|p!=shred_parent) { return Err(invalid()); }
        parent = Some(shred_parent);
        group.push(shred.payload().as_ref());
        if shred.data_complete() {
            let payload = Shredder::deshred(group.drain(..)).map_err(|_|invalid())?;
            let component: BlockComponent = wincode::deserialize(&payload).map_err(|_|invalid())?;
            if let BlockComponent::EntryBatch(entries) = component {
                for entry in entries {
                    for tx in entry.transactions {
                        signatures.push(tx.signatures.first().ok_or_else(invalid)?.to_string());
                        transactions.push(serde_json::to_value(&tx).map_err(|_|invalid())?);
                    }
                }
            }
        }
    }
    if !group.is_empty() { return Err(invalid()); }
    result["parent_slot"] = json!(parent);
    result["transaction_signatures"] = json!(signatures);
    result["ordered_transactions"] = json!(transactions);
    result["reconstructed"] = json!(true);
    // Immutable evidence first; checkpoint advancement happens only after this fsync.
    atomic_file(&dir.join("handoff.json"),serde_json::to_vec(&result).map_err(|_|invalid())?.as_slice())?;
    Ok(result)
}

pub fn acquire(config_path: &Path, dir: &Path) -> io::Result<()> {
    let config = read_json(config_path)?;
    let duration = bounded(&config,"duration_seconds",1,50)?;
    let max = bounded(&config,"max_shreds_per_slot",1,4096)? as u32;
    let version = bounded(&config,"shred_version",1,65535)? as u16;
    let budget = bounded(&config,"max_namespace_bytes",1,1_000_000)?;
    let listen: SocketAddr = config["listen"].as_str().ok_or_else(invalid)?.parse().map_err(|_|invalid())?;
    if !listen.is_ipv4() || listen.port()<1024 || listen.port()==8899 || listen.port()==8900 { return Err(invalid()); }
    let senders = config["senders"].as_array().ok_or_else(invalid)?.iter().map(|v|
        v.as_str().ok_or_else(invalid)?.parse::<SocketAddr>().map_err(|_|invalid())).collect::<io::Result<Vec<_>>>()?;
    if senders.is_empty() || senders.len()>4 { return Err(invalid()); }
    let rows = config["slots"].as_array().ok_or_else(invalid)?;
    if rows.is_empty() || rows.len()>4 { return Err(invalid()); }
    let mut slots = BTreeMap::new();
    for row in rows {
        let slot = row["slot"].as_u64().ok_or_else(invalid)?;
        let leader = Pubkey::from_str(row["leader"].as_str().ok_or_else(invalid)?).map_err(|_|invalid())?;
        if slots.insert(slot,leader).is_some() { return Err(invalid()); }
    }
    let _lock = private_dir(dir)?;
    if dir.join("checkpoint.json").exists()
        && read_json(&dir.join("checkpoint.json"))?["format"] != "freya-native-acquisition-v1" {
        return Err(invalid());
    }
    let policy = json!({"shred_version":version,"slots":rows,"max_shreds_per_slot":max});
    let policy_path = dir.join("acquisition-policy.json");
    if policy_path.exists() {
        if read_json(&policy_path)?!=policy { return Err(invalid()); }
    } else { atomic_file(&policy_path,serde_json::to_vec(&policy).map_err(|_|invalid())?.as_slice())?; }
    for slot in slots.keys() {
        let path = dir.join(slot.to_string());
        if !path.exists() { fs::create_dir(&path)?; fs::set_permissions(&path,fs::Permissions::from_mode(0o700))?; }
        if !fs::symlink_metadata(&path)?.is_dir() { return Err(invalid()); }
    }
    lease()?;
    let initial = counters()?;
    let socket = UdpSocket::bind(listen)?;
    socket.set_read_timeout(Some(Duration::from_millis(100)))?;
    let stop = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(signal_hook::consts::SIGINT,stop.clone())?;
    signal_hook::flag::register(signal_hook::consts::SIGTERM,stop.clone())?;
    let started = Instant::now();
    let mut status = json!({"format":"freya-native-acquisition-v1","schema_version":1,"state":"starting",
        "native_ready":false,"received_shreds":0,"duplicate_shreds":0,"discarded_datagrams":0,
        "repair_attempts":0,"repair_supported":false,"execution_metadata":null,"market_data_ready":false,
        "source":"native_udp","finality":"unverified","handoff_state":"quarantined",
        "ram_bytes":null,"cpu_seconds":null,"last_error_category":null});
    // Rescan durable files at startup; a crash cannot skip an incomplete slot.
    let snapshot = |status: &mut Value| -> io::Result<()> {
        let mut reports = Vec::new();
        for (slot,leader) in &slots {
            let mut report = reconstruct(&dir.join(slot.to_string()),*slot,version,leader,max)?;
            if let Some(object) = report.as_object_mut() {
                object.remove("ordered_transactions"); object.remove("transaction_signatures");
            }
            reports.push(report);
        }
        status["slots"] = json!(reports);
        status["last_fully_reconstructed_slot"] = json!(reports.iter()
            .filter(|r|r["reconstructed"]==true).filter_map(|r|r["slot"].as_u64()).max());
        status["last_verified_slot"] = Value::Null;
        status["native_source_ready"] = json!(false);
        status["updated_at"] = json!(now());
        publish(dir,status)
    };
    let outcome = (|| -> io::Result<()> {
        snapshot(&mut status)?;
        if slots.keys().any(|slot|dir.join(slot.to_string()).join("conflict.json").exists()) {
            return Err(io::Error::other("unresolved conflicting evidence"));
        }
        let mut packet = [0u8;2049];
        while started.elapsed()<Duration::from_secs(duration) && !stop.load(Ordering::Relaxed) {
            lease()?;
            let traffic = counters()?;
            let rx = traffic.0.checked_sub(initial.0).ok_or_else(invalid)?;
            let tx = traffic.1.checked_sub(initial.1).ok_or_else(invalid)?;
            status["namespace_incoming_bytes"] = json!(rx); status["namespace_outgoing_bytes"] = json!(tx);
            if rx.saturating_add(tx)>=budget { return Err(io::Error::other("traffic budget")); }
            match socket.recv_from(&mut packet) {
                Ok((length,sender)) => {
                    let parsed = if length<=2048 && senders.contains(&sender) {
                        Shred::new_from_serialized_shred(packet[..length].to_vec()).ok()
                    } else { None };
                    if let Some(shred) = parsed.filter(|s|slots.contains_key(&s.slot())) {
                        let slot = shred.slot();
                        if let Ok(shred) = checked(packet[..length].to_vec(),slot,version,&slots[&slot],max) {
                            let base = dir.join(slot.to_string());
                            let path = base.join(format!("{}.shred",shred.index()));
                            if path.exists() {
                                if raw(&path)?!=packet[..length] {
                                    atomic_file(&base.join("conflict.json"),b"{\"conflict\":true}")?;
                                    // Keep the conflicting payload; stop rather than overwrite evidence.
                                    atomic_file(&base.join("conflicting-shred.bin"),&packet[..length])?;
                                    return Err(io::Error::other("conflicting shred"));
                                }
                                status["duplicate_shreds"] = json!(status["duplicate_shreds"].as_u64().unwrap_or(0)+1);
                            } else {
                                atomic_file(&path,&packet[..length])?;
                                status["received_shreds"] = json!(status["received_shreds"].as_u64().unwrap_or(0)+1);
                                status["last_received_slot"] = json!(slot);
                            }
                        } else { status["discarded_datagrams"] = json!(status["discarded_datagrams"].as_u64().unwrap_or(0)+1); }
                    } else { status["discarded_datagrams"] = json!(status["discarded_datagrams"].as_u64().unwrap_or(0)+1); }
                }
                Err(e) if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut) => (),
                Err(e) => return Err(e),
            }
            status["state"] = json!("syncing");
            snapshot(&mut status)?;
        }
        Ok(())
    })();
    status["state"] = json!(if outcome.is_ok(){"stopped"}else{"blocked"});
    if outcome.is_err() { status["last_error_category"] = json!("acquisition_or_policy_failure"); }
    // Best-effort rescan exposes conflicts; retain earlier checkpoint if reconstruction itself failed.
    if snapshot(&mut status).is_err() { publish(dir,&status)?; }
    outcome
}
