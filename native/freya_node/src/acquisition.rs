//! Bounded UDP shred acquisition. Native records are quarantined, not execution receipts.
use super::*;
use solana_entry::block_component::BlockComponent;
use solana_entry::entry::Entry;
use solana_hash::Hash;
use solana_ledger::shred::{Shred, Shredder};
use solana_pubkey::Pubkey;
use std::{collections::BTreeMap, net::UdpSocket, str::FromStr};

fn verify_entry_chain(record: &Value, anchor: &Value) -> io::Result<Value> {
    let slot = record["slot"].as_u64().ok_or_else(invalid)?;
    let parent_slot = record["parent_slot"].as_u64().ok_or_else(invalid)?;
    if record["schema"] != "freya-native-slot-v1" || record["reconstructed"] != true
        || record["conflict"] != false || anchor["schema"] != "freya-trusted-poh-anchor-v1"
        || anchor["slot"].as_u64() != Some(slot) || anchor["parent_slot"].as_u64() != Some(parent_slot)
        || parent_slot >= slot {
        return Err(invalid());
    }
    let parse_hash = |value: &Value| {
        Hash::from_str(value.as_str().ok_or_else(invalid)?).map_err(|_| invalid())
    };
    let mut previous = parse_hash(&anchor["parent_entry_hash"])?;
    let terminal = parse_hash(&anchor["terminal_entry_hash"])?;
    let transactions = record["ordered_transactions"].as_array().ok_or_else(invalid)?;
    let components = record["replay_components"].as_array().ok_or_else(invalid)?;
    let mut cursor = 0usize;
    let mut hashes = 0u64;
    let mut entries = 0usize;
    for component in components {
        match component["kind"].as_str() {
            Some("block_marker") => continue,
            Some("entry_batch") => {}
            _ => return Err(invalid()),
        }
        for row in component["entries"].as_array().ok_or_else(invalid)? {
            let first = row["first_transaction"].as_u64().and_then(|n| usize::try_from(n).ok()).ok_or_else(invalid)?;
            let count = row["transaction_count"].as_u64().and_then(|n| usize::try_from(n).ok()).ok_or_else(invalid)?;
            if first != cursor { return Err(invalid()); }
            let end = first.checked_add(count).ok_or_else(invalid)?;
            let num_hashes = row["num_hashes"].as_u64().ok_or_else(invalid)?;
            hashes = hashes.checked_add(num_hashes).filter(|n| *n <= 16_000_000)
                .ok_or_else(|| io::Error::other("native PoH verification work limit exceeded"))?;
            let entry = Entry {
                num_hashes,
                hash: parse_hash(&row["hash"])?,
                transactions: transactions.get(first..end).ok_or_else(invalid)?.iter()
                    .map(|tx| serde_json::from_value(tx.clone()).map_err(|_| invalid()))
                    .collect::<io::Result<Vec<_>>>()?,
            };
            if !entry.verify(&previous) {
                return Err(io::Error::other("native PoH entry hash mismatch"));
            }
            previous = entry.hash;
            cursor = end;
            entries += 1;
        }
    }
    if entries == 0 || cursor != transactions.len() || previous != terminal {
        return Err(io::Error::other("native PoH terminal anchor mismatch"));
    }
    Ok(json!({"slot": slot, "parent_slot": parent_slot, "entries": entries,
        "hashes": hashes, "terminal_entry_hash": terminal.to_string(),
        "verification": "poh_against_supplied_anchor", "finality": "unverified",
        "execution_metadata": null, "market_data_ready": false}))
}

pub(super) fn verify_poh(anchor: &Path, dir: &Path) -> io::Result<()> {
    if !fs::symlink_metadata(dir)?.is_dir() || dir.join("conflict.json").exists() {
        return Err(invalid());
    }
    let mut file = OpenOptions::new().read(true).custom_flags(0x20800).open(dir.join("handoff.json"))?;
    if !file.metadata()?.is_file() { return Err(invalid()); }
    const LIMIT: u64 = 32 * 1024 * 1024;
    let mut bytes = Vec::new();
    Read::by_ref(&mut file).take(LIMIT + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > LIMIT { return Err(invalid()); }
    let record = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
    let result = verify_entry_chain(&record, &read_json(anchor)?)?;
    if dir.join("conflict.json").exists() { return Err(invalid()); }
    println!("{result}");
    Ok(())
}

pub(super) fn export_replay_ledger(config: &Path, dir: &Path, ledger: &Path) -> io::Result<()> {
    let config = read_json(config)?;
    let slot = bounded(&config, "slot", 1, i64::MAX as u64 - 1)?;
    let parent = bounded(&config, "parent_slot", 0, slot - 1)?;
    let version = bounded(&config, "shred_version", 1, 65535)? as u16;
    let leader = Pubkey::from_str(config["leader"].as_str().ok_or_else(invalid)?).map_err(|_| invalid())?;
    if !fs::symlink_metadata(dir)?.is_dir() || dir.join("conflict.json").exists() {
        return Err(invalid());
    }
    let mut shreds = Vec::new();
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("shred") {
            continue;
        }
        if shreds.len() >= 4096 { return Err(invalid()); }
        let shred = checked(raw(&path)?, slot, version, &leader, 4096)?;
        if shred.parent().map_err(|_| invalid())? != parent { return Err(invalid()); }
        shreds.push(shred);
    }
    shreds.sort_by_key(Shred::index);
    if shreds.is_empty() || dir.join("conflict.json").exists()
        || shreds.iter().enumerate().any(|(index, shred)| {
            shred.index() as usize != index || shred.last_in_slot() != (index + 1 == shreds.len())
        }) {
        return Err(io::Error::other("complete conflict-free slot shreds required"));
    }
    std::fs::DirBuilder::new().mode(0o700).create(ledger)?;
    let blockstore = solana_ledger::blockstore::Blockstore::open(ledger)
        .map_err(|_| io::Error::other("unable to open new replay blockstore"))?;
    blockstore.insert_shreds(shreds, false)
        .map_err(|_| io::Error::other("replay blockstore rejected shreds"))?;
    if !blockstore.is_full(slot) {
        return Err(io::Error::other("replay blockstore has an incomplete slot"));
    }
    println!("{}", json!({"slot": slot, "parent_slot": parent, "replay_input": "complete",
        "finality": "unverified", "execution_metadata": null}));
    Ok(())
}

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
    let file = OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > 2048 { return Err(invalid()); }
    let mut bytes = Vec::new();
    file.take(2049).read_to_end(&mut bytes)?;
    if bytes.len() > 2048 { return Err(invalid()); }
    Ok(bytes)
}
pub(super) fn checked(bytes: Vec<u8>, slot: u64, version: u16, leader: &Pubkey, max: u32) -> io::Result<Shred> {
    let shred = checked_any(bytes,slot,version,leader,max)?;
    if !shred.is_data() { return Err(invalid()); }
    Ok(shred)
}
fn checked_any(bytes: Vec<u8>, slot: u64, version: u16, leader: &Pubkey, max: u32) -> io::Result<Shred> {
    let shred = Shred::new_from_serialized_shred(bytes).map_err(|_| invalid())?;
    shred.sanitize().map_err(|_| invalid())?;
    if shred.slot()!=slot || shred.version()!=version
        || shred.index()>=max || !shred.verify(leader) { return Err(invalid()); }
    Ok(shred)
}
pub(super) fn reconstruct(dir: &Path, slot: u64, version: u16, leader: &Pubkey, max: u32) -> io::Result<Value> {
    let mut shreds = BTreeMap::new();
    let mut fec = BTreeMap::<u32,Vec<Shred>>::new();
    for row in fs::read_dir(dir)? {
        let path = row?.path();
        let extension = path.extension().and_then(|s|s.to_str());
        if extension != Some("shred") && extension != Some("coding") { continue; }
        let shred = checked_any(raw(&path)?,slot,version,leader,max)?;
        if shred.is_data() != (extension==Some("shred")) { return Err(invalid()); }
        fec.entry(shred.fec_set_index()).or_default().push(shred.clone());
        if shred.is_data() && shreds.insert(shred.index(),shred).is_some() { return Err(invalid()); }
    }
    let mut recovered = 0u64;
    static CACHE: std::sync::OnceLock<solana_ledger::shred::ReedSolomonCache> = std::sync::OnceLock::new();
    if !dir.join("conflict.json").exists() {
        for group in fec.into_values().filter(|g|g.iter().any(|s|!s.is_data())) {
            if let Ok(output) = solana_ledger::shred::freya_recover(group,CACHE.get_or_init(Default::default)) {
                for shred in output.into_iter().filter(Shred::is_data) {
                    let bytes = shred.payload().as_ref().to_vec();
                    let shred = checked(bytes.clone(),slot,version,leader,max)?;
                    if let Some(existing) = shreds.get(&shred.index()) {
                        if existing.payload().as_ref()!=bytes.as_slice() { return Err(invalid()); }
                    } else {
                        atomic_file(&dir.join(format!("{}.shred",shred.index())),&bytes)?;
                        shreds.insert(shred.index(),shred); recovered+=1;
                    }
                }
            }
        }
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
        "terminal_index":last.first(),"reconstructed":false,"execution_metadata":null,"recovered_data_shreds":recovered,
        "finality":"unverified","verification":"operator_configured_leader_signature",
        "authoritative":false,"conflict":dir.join("conflict.json").exists()});
    if !complete || result["conflict"]==true { return Ok(result); }
    let mut group = Vec::new();
    let mut transactions = Vec::new();
    let mut signatures = Vec::new();
    let mut components = Vec::new();
    let mut parent = None;
    for shred in shreds.values() {
        let shred_parent = shred.parent().map_err(|_|invalid())?;
        if parent.is_some_and(|p|p!=shred_parent) { return Err(invalid()); }
        parent = Some(shred_parent);
        group.push(shred.payload().as_ref());
        if shred.data_complete() {
            let payload = Shredder::deshred(group.drain(..)).map_err(|_|invalid())?;
            let component: BlockComponent = wincode::deserialize(&payload).map_err(|_|invalid())?;
            match component {
                BlockComponent::EntryBatch(entries) => {
                    let mut batch = Vec::with_capacity(entries.len());
                    for entry in entries {
                        let first = transactions.len();
                        for tx in entry.transactions {
                            signatures.push(tx.signatures.first().ok_or_else(invalid)?.to_string());
                            transactions.push(serde_json::to_value(&tx).map_err(|_|invalid())?);
                        }
                        batch.push(json!({
                            "num_hashes": entry.num_hashes, "hash": entry.hash.to_string(),
                            "first_transaction": first, "transaction_count": transactions.len() - first,
                        }));
                    }
                    components.push(json!({"kind": "entry_batch", "entries": batch}));
                }
                // Preserve the exact wire marker, including any candidate
                // certificate, for a verifier with trusted epoch stake state.
                BlockComponent::BlockMarker(_) => components.push(json!({
                    "kind": "block_marker", "wire_bytes": payload,
                })),
            }
        }
    }
    if !group.is_empty() { return Err(invalid()); }
    result["parent_slot"] = json!(parent);
    result["transaction_signatures"] = json!(signatures);
    result["ordered_transactions"] = json!(transactions);
    result["replay_components"] = json!(components);
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
                object.remove("replay_components");
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
                        if let Ok(shred) = checked_any(packet[..length].to_vec(),slot,version,&slots[&slot],max) {
                            let base = dir.join(slot.to_string());
                            let suffix = if shred.is_data(){"shred"}else{"coding"};
                            let path = base.join(format!("{}.{suffix}",shred.index()));
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

#[cfg(test)]
pub(super) mod reconstruction_tests {
    use super::*;
    use solana_entry::entry::Entry;
    use solana_ledger::shred::{ProcessShredsStats, ReedSolomonCache};

    #[test]
    fn verifies_poh_against_parent_and_terminal_hashes_without_granting_finality() {
        let parent = Hash::default();
        let entry = Entry::new(&parent, 1, vec![]);
        let anchor = json!({"schema":"freya-trusted-poh-anchor-v1","slot":42,"parent_slot":41,
            "parent_entry_hash":parent.to_string(),"terminal_entry_hash":entry.hash.to_string()});
        let mut record = json!({"schema":"freya-native-slot-v1","slot":42,"parent_slot":41,
            "reconstructed":true,"conflict":false,"ordered_transactions":[],
            "replay_components":[{"kind":"entry_batch","entries":[{
                "num_hashes":entry.num_hashes,"hash":entry.hash.to_string(),
                "first_transaction":0,"transaction_count":0}]}]});
        let report = verify_entry_chain(&record, &anchor).unwrap();
        assert_eq!(report["finality"], "unverified");
        record["replay_components"][0]["entries"][0]["hash"] = json!(parent.to_string());
        assert!(verify_entry_chain(&record, &anchor).is_err());
        record["replay_components"][0]["entries"][0]["num_hashes"] = json!(16_000_001);
        assert!(verify_entry_chain(&record, &anchor).is_err());
    }
    pub(crate) fn fixture() -> (Keypair,Vec<Shred>) {
        let key=Keypair::new();
        let component=BlockComponent::EntryBatch(vec![Entry{num_hashes:1,hash:Default::default(),transactions:vec![]}]);
        let payload=wincode::serialize(&component).unwrap();
        let shreds=Shredder::new(42,41,0,7).unwrap().make_shreds_from_data_slice(
            &key,&payload,true,Default::default(),0,0,&ReedSolomonCache::default(),
            &mut ProcessShredsStats::default()).unwrap().collect();
        (key,shreds)
    }
    fn put(dir:&Path,shred:&Shred) {
        let suffix=if shred.is_data(){"shred"}else{"coding"};
        atomic_file(&dir.join(format!("{}.{suffix}",shred.index())),shred.payload().as_ref()).unwrap();
    }
    #[test]
    fn exports_complete_signed_slot_to_new_replay_blockstore() {
        let (leader, shreds) = fixture();
        let root = tempfile::tempdir().unwrap();
        let input = root.path().join("shreds");
        fs::create_dir(&input).unwrap();
        let config = root.path().join("anchor.json");
        fs::write(&config, json!({"slot":42,"parent_slot":41,"shred_version":7,
            "leader":leader.pubkey().to_string()}).to_string()).unwrap();
        let ledger = root.path().join("ledger");
        assert!(export_replay_ledger(&config, &input, &ledger).is_err());
        assert!(!ledger.exists());
        for shred in shreds.iter().filter(|shred| shred.is_data()) {
            put(&input, shred);
        }
        export_replay_ledger(&config, &input, &ledger).unwrap();
        assert!(export_replay_ledger(&config, &input, &ledger).is_err());
        let blockstore = solana_ledger::blockstore::Blockstore::open(&ledger).unwrap();
        assert!(blockstore.is_full(42));
    }
    #[test] fn complete_data_reconstructs_without_receipts() {
        let (key,shreds)=fixture();let dir=tempfile::tempdir().unwrap();
        for shred in shreds.iter().filter(|s|s.is_data()){put(dir.path(),shred);}
        let report=reconstruct(dir.path(),42,7,&key.pubkey(),4096).unwrap();
        assert_eq!(report["reconstructed"],true);assert_eq!(report["execution_metadata"],Value::Null);
        assert_eq!(report["parent_slot"],41);
        assert_eq!(report["replay_components"], json!([{
            "kind":"entry_batch","entries":[{
                "num_hashes":1,"hash":Entry::default().hash.to_string(),
                "first_transaction":0,"transaction_count":0
            }]
        }]));
    }
    #[test] fn missing_data_stays_quarantined_then_recovers_from_parity() {
        let (key,shreds)=fixture();let dir=tempfile::tempdir().unwrap();
        for shred in shreds.iter().filter(|s|s.is_data() && s.index()!=0){put(dir.path(),shred);}
        assert_eq!(reconstruct(dir.path(),42,7,&key.pubkey(),4096).unwrap()["reconstructed"],false);
        for shred in shreds.iter().filter(|s|!s.is_data()){put(dir.path(),shred);}
        let report=reconstruct(dir.path(),42,7,&key.pubkey(),4096).unwrap();
        assert_eq!(report["reconstructed"],true);
        assert!(report["recovered_data_shreds"].as_u64().unwrap()>0);
        assert_eq!(reconstruct(dir.path(),42,7,&key.pubkey(),4096).unwrap()["reconstructed"],true);
    }
    #[test] fn wrong_leader_version_and_duplicate_records_fail() {
        let (key,shreds)=fixture();let data=shreds.iter().find(|s|s.is_data()).unwrap();
        assert!(checked(data.payload().as_ref().to_vec(),42,8,&key.pubkey(),4096).is_err());
        assert!(checked(data.payload().as_ref().to_vec(),42,7,&Keypair::new().pubkey(),4096).is_err());
        let dir=tempfile::tempdir().unwrap();put(dir.path(),data);
        atomic_file(&dir.path().join("duplicate.shred"),data.payload().as_ref()).unwrap();
        assert!(reconstruct(dir.path(),42,7,&key.pubkey(),4096).is_err());
    }
}
