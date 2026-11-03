//! Compiled inside the pinned Agave workspace; never admits market evidence.
#![forbid(unsafe_code)]
use solana_gossip::{crds::Cursor, gossip_service::make_node};
use solana_keypair::{read_keypair_file, Keypair};
use solana_net_utils::SocketAddrSpace;
use solana_signer::Signer;
use serde_json::{json, Value};
use std::{env, fs::{self, File, OpenOptions}, io::{self, Read, Write}, net::SocketAddr,
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt}, path::{Path, PathBuf},
    sync::{Arc, atomic::{AtomicBool, Ordering}}, thread, time::{Duration, Instant, SystemTime, UNIX_EPOCH}};
const FORMAT: &str = "freya-peer-v1";
mod acquisition;
fn invalid() -> io::Error { io::Error::other("invalid private configuration or state") }
fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() }
fn read_json(path: &Path) -> io::Result<Value> {
    let mut file = OpenOptions::new().read(true).custom_flags(0x20000).open(path)?;
    if file.metadata()?.len() > 1_048_576 { return Err(invalid()); }
    let mut bytes = Vec::new(); file.read_to_end(&mut bytes)?;
    serde_json::from_slice(&bytes).map_err(|_| invalid())
}
fn publish(dir: &Path, value: &Value) -> io::Result<()> {
    let mut file = OpenOptions::new().write(true).create(true).truncate(true)
        .mode(0o600).custom_flags(0x20000).open(dir.join("checkpoint.pending"))?;
    file.write_all(serde_json::to_string(value).map_err(|_| invalid())?.as_bytes())?;
    file.sync_all()?; fs::rename(dir.join("checkpoint.pending"), dir.join("checkpoint.json"))?;
    File::open(dir)?.sync_all()
}
fn bounded(value: &Value, key: &str, min: u64, max: u64) -> io::Result<u64> {
    value[key].as_u64().filter(|v| (min..=max).contains(v)).ok_or_else(invalid)
}
fn private_dir(dir: &Path) -> io::Result<File> {
    let meta = fs::symlink_metadata(dir)?;
    if !meta.is_dir() || meta.uid() != fs::metadata("/proc/self")?.uid()
        || meta.permissions().mode() & 0o077 != 0 { return Err(invalid()); }
    // Existing unowned directories and databases are never adopted.
    let marker = dir.join("owner.json");
    if !marker.exists() {
        if fs::read_dir(dir)?.next().is_some() { return Err(invalid()); }
        let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&marker)?;
        file.write_all(b"{\"format\":\"freya-peer-v1\"}")?; file.sync_all()?;
    }
    if read_json(&marker)?["format"] != FORMAT { return Err(invalid()); }
    let lock = OpenOptions::new().read(true).write(true).create(true).truncate(false)
        .mode(0o600).custom_flags(0x20000).open(dir.join("writer.lock"))?;
    lock.try_lock().map_err(|_| io::Error::other("peer state already locked"))?;
    Ok(lock)
}
fn lease() -> io::Result<Value> {
    let path = Path::new("/run/solana-budget-status.json");
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.uid() != 0 || meta.permissions().mode() & 0o022 != 0 { return Err(invalid()); }
    let status = read_json(path)?;
    if status["namespace_inode"].as_u64() != Some(fs::metadata("/proc/self/ns/net")?.ino())
        || status["optional_allowed"] != true || status["expires"].as_f64().unwrap_or(0.) <= now() as f64 {
        return Err(io::Error::other("authorized isolated network lease required"));
    }
    Ok(status)
}
fn counters() -> io::Result<(u64,u64)> {
    let read = |name: &str| -> io::Result<u64> {
        fs::read_to_string(format!("/sys/class/net/eth0/statistics/{name}"))?
            .trim().parse().map_err(|_| invalid())
    };
    Ok((read("rx_bytes")?,read("tx_bytes")?))
}
fn run() -> io::Result<()> {
    let args: Vec<_> = env::args_os().skip(1).collect();
    if args.len() != 3 { return Err(io::Error::other("usage: freya-peer (discover|status) CONFIG STATE_DIRECTORY")); }
    let dir = PathBuf::from(&args[2]);
    if args[0] == "acquire" { return acquisition::acquire(Path::new(&args[1]), &dir); }
    if args[0] == "status" {
        let value = read_json(&dir.join("checkpoint.json"))?;
        if value["format"] != FORMAT && value["format"] != "freya-native-acquisition-v1" { return Err(invalid()); }
        println!("{value}"); return Ok(());
    }
    if args[0] != "discover" { return Err(invalid()); }
    let config = read_json(Path::new(&args[1]))?;
    let seconds = bounded(&config,"duration_seconds",1,50)?;
    let limit = bounded(&config,"max_peers",1,256)? as usize;
    let bytes = bounded(&config,"max_namespace_bytes",1,1_000_000)?;
    let shred_version = bounded(&config,"shred_version",1,65535)? as u16;
    let entrypoints: Vec<SocketAddr> = config["bootstrap_peers"].as_array().ok_or_else(invalid)?
        .iter().map(|v| v.as_str().ok_or_else(invalid)?.parse().map_err(|_| invalid())).collect::<io::Result<_>>()?;
    if entrypoints.is_empty() || entrypoints.len() > 4 || entrypoints.iter().any(|p|
        !p.is_ipv4() || p.ip().is_loopback() || p.ip().is_unspecified() || p.port() == 0) { return Err(invalid()); }
    let _lock = private_dir(&dir)?;
    let checkpoint = dir.join("checkpoint.json");
    let mut state = if checkpoint.exists() { read_json(&checkpoint)? } else { json!({
        "format":FORMAT,"schema_version":1,"sessions":0,"observed_network_slot":null,
        "last_network_progress":null,"recovered_ranges":[],"missing_ranges":null
    }) };
    if !state.is_object() || state["format"] != FORMAT || state["schema_version"] != 1 { return Err(invalid()); }
    if let Some(previous) = state["configured_shred_version"].as_u64() {
        if previous != u64::from(shred_version) { return Err(invalid()); }
    }
    state["configured_shred_version"] = json!(shred_version);
    state["network_identity"] = json!("operator-configured bootstrap and shred version; genesis not verified");
    state["previous_observed_slot"] = state["observed_network_slot"].clone();
    state["observed_network_slot"] = Value::Null;
    let identity = dir.join("identity.json");
    if !identity.exists() {
        let key = Keypair::new();
        let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&identity)?;
        file.write_all(serde_json::to_string(&key.to_bytes().to_vec()).map_err(|_| invalid())?.as_bytes())?;
        file.sync_all()?; File::open(&dir)?.sync_all()?;
    }
    let meta = fs::symlink_metadata(&identity)?;
    if !meta.is_file() || meta.uid() != fs::metadata("/proc/self")?.uid() || meta.permissions().mode() & 0o077 != 0 { return Err(invalid()); }
    let key = read_keypair_file(&identity).map_err(|_| invalid())?;
    state["node_identity"] = json!(key.pubkey().to_string());
    state["previous_session_state"] = state["state"].clone();
    state["state"] = json!("starting");
    state["native_ready"] = json!(false);
    state["verification_level"] = json!("authenticated_peer_claims_only");
    state["independent_consensus_verified"] = json!(false);
    state["external_fallback"] = json!("unchanged; native observations quarantined");
    state["engine_version"] = json!("freya-peer-0.2.0/agave-825efd18");
    state["bootstrap_peers"] = json!(entrypoints.iter().map(ToString::to_string).collect::<Vec<_>>());
    let old_sessions = state["sessions"].as_u64().ok_or_else(invalid)?;
    state["sessions"] = json!(old_sessions.checked_add(1).ok_or_else(invalid)?);
    // Persist before any socket opens; last durable claims survive an interrupted session.
    publish(&dir,&state)?;
    if lease().is_err() {
        state["state"] = json!("blocked");
        state["last_error_category"] = json!("network_authorization");
        publish(&dir,&state)?;
        return Err(invalid());
    }
    let initial = counters()?;
    let stop = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(signal_hook::consts::SIGINT,stop.clone())?;
    signal_hook::flag::register(signal_hook::consts::SIGTERM,stop.clone())?;
    let (service, _, cluster) = make_node(key,&entrypoints,stop.clone(),None,shred_version,true,SocketAddrSpace::Global);
    let started = Instant::now();
    let mut cursor = Cursor::default();
    let result = (|| -> io::Result<()> {
        while started.elapsed() < Duration::from_secs(seconds) && !stop.load(Ordering::Relaxed) {
            lease()?;
            let traffic = counters()?;
            let incoming = traffic.0.checked_sub(initial.0).ok_or_else(invalid)?;
            let outgoing = traffic.1.checked_sub(initial.1).ok_or_else(invalid)?;
            if incoming.saturating_add(outgoing) >= bytes { return Err(io::Error::other("namespace traffic bound reached")); }
            let peers: Vec<_> = cluster.all_peers().into_iter().filter(|(p,stamp)|
                p.pubkey() != &cluster.id() && p.shred_version() == shred_version
                && now().saturating_mul(1000).saturating_sub(*stamp) <= 30_000).collect();
            if peers.len() > limit { return Err(io::Error::other("compatible peer capacity reached")); }
            let mut high = state["observed_network_slot"].as_u64();
            for report in cluster.get_epoch_slots(&mut cursor) {
                if !peers.iter().any(|(p,_)| p.pubkey() == &report.from) { continue; }
                for slot in report.to_slots(0) { high = Some(high.unwrap_or(0).max(slot)); }
            }
            if high != state["observed_network_slot"].as_u64() { state["last_network_progress"] = json!(now()); }
            state["observed_network_slot"] = json!(high);
            state["state"] = json!(if peers.is_empty() {"discovering"} else {"degraded"});
            state["peer_count"] = json!(peers.len());
            state["peers"] = json!(peers.iter().map(|(p,stamp)| json!({"identity":p.pubkey().to_string(),
                "gossip":p.gossip().map(|a|a.to_string()),"last_seen_ms":stamp,"shred_version":p.shred_version()})).collect::<Vec<_>>());
            state["namespace_incoming_bytes"] = json!(incoming);
            state["namespace_outgoing_bytes"] = json!(outgoing);
            state["namespace_bytes_per_second"] = json!((incoming+outgoing) as f64 / started.elapsed().as_secs_f64().max(0.001));
            state["updated_at"] = json!(now());
            for field in ["gossip_bytes","repair_bytes","ledger_bytes","duplicates","network_errors",
                "reconnections","ram_bytes","cpu_seconds","queue_pressure","reconstructed_data_bytes"] { state[field] = Value::Null; }
            state["last_error_category"] = Value::Null;
            publish(&dir,&state)?;
            thread::sleep(Duration::from_millis(100));
        }
        Ok(())
    })();
    stop.store(true,Ordering::Relaxed);
    let joined = service.join();
    state["state"] = json!(if result.is_ok() && joined.is_ok() {"stopped"} else {"blocked"});
    if result.is_err() || joined.is_err() { state["last_error_category"] = json!("network_policy_or_resource_bound"); }
    publish(&dir,&state)?;
    result?;
    joined.map_err(|_| io::Error::other("gossip worker failed"))
}
fn main() {
    if run().is_err() { eprintln!("Native discovery stopped; configuration, authorization or resource failure (details omitted)"); std::process::exit(1); }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn bounds_fail_closed() {
        assert!(bounded(&json!({"limit":0}),"limit",1,256).is_err());
        assert!(bounded(&json!({"limit":257}),"limit",1,256).is_err());
        assert_eq!(bounded(&json!({"limit":10}),"limit",1,256).unwrap(),10);
    }
    #[test] fn checkpoint_retains_unresolved_claims_across_restart() {
        let dir = tempfile::tempdir().unwrap();
        let lock = private_dir(dir.path()).unwrap();
        let state = json!({"format":FORMAT,"state":"discovering","observed_network_slot":42,
            "native_ready":false,"recovered_ranges":[],"missing_ranges":null});
        publish(dir.path(),&state).unwrap();
        assert!(private_dir(dir.path()).is_err());
        drop(lock);
        let _lock = private_dir(dir.path()).unwrap();
        assert_eq!(read_json(&dir.path().join("checkpoint.json")).unwrap(),state);
        assert!(!dir.path().join("checkpoint.pending").exists());
    }
    #[test] fn refuses_unowned_data_without_deleting_it() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("index.sqlite"),b"existing evidence").unwrap();
        assert!(private_dir(dir.path()).is_err());
        assert_eq!(fs::read(dir.path().join("index.sqlite")).unwrap(),b"existing evidence");
    }
    #[test] fn symlinked_checkpoint_is_not_followed() {
        let dir = tempfile::tempdir().unwrap();
        let outside = tempfile::NamedTempFile::new().unwrap();
        std::os::unix::fs::symlink(outside.path(),dir.path().join("checkpoint.pending")).unwrap();
        assert!(publish(dir.path(),&json!({"format":FORMAT})).is_err());
        assert_eq!(outside.as_file().metadata().unwrap().len(),0);
    }
}
