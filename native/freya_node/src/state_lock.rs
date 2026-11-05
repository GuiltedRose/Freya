//! Exclusive checkpoint writer ownership, independent of inherited descriptors.
use std::{fs::File, io};

pub(super) struct StateLock(File);
impl StateLock {
    pub(super) fn acquire(file: File) -> io::Result<Self> {
        file.try_lock().map_err(|_| io::Error::other("peer state already locked"))?;
        Ok(Self(file))
    }
}
impl Drop for StateLock {
    fn drop(&mut self) {
        // flock belongs to the open file description. Close alone can leave it
        // held by a concurrent fork until exec/exit closes the inherited copy.
        // Only a successfully acquired guard may unlock; a failed contender must
        // never release the current owner's lock. Closing remains the fallback.
        let _ = self.0.unlock();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs::{self, OpenOptions}, path::PathBuf, process::Command,
              sync::atomic::{AtomicU64, Ordering}, thread, time::{Duration, Instant}};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("freya-state-lock-{}-{}",
                std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn file(&self) -> File {
            OpenOptions::new().read(true).write(true).create(true).truncate(false)
                .open(self.0.join("writer.lock")).unwrap()
        }
    }
    impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }

    #[test]
    fn owner_excludes_contenders_until_guard_drops() {
        let dir = Fixture::new();
        let owner = StateLock::acquire(dir.file()).unwrap();
        assert!(StateLock::acquire(dir.file()).is_err());
        assert!(StateLock::acquire(dir.file()).is_err());
        drop(owner);
        let next = StateLock::acquire(dir.file()).unwrap();
        assert!(StateLock::acquire(dir.file()).is_err());
        drop(next);
        assert!(StateLock::acquire(dir.file()).is_ok());
    }

    #[test]
    fn release_does_not_wait_for_duplicate_descriptor_to_close() {
        let dir = Fixture::new();
        let file = dir.file();
        // A duplicate shares exactly the open file description inherited by fork.
        let duplicate = file.try_clone().unwrap();
        let owner = StateLock::acquire(file).unwrap();
        assert!(StateLock::acquire(dir.file()).is_err());
        drop(owner);
        let next = StateLock::acquire(dir.file()).unwrap();
        drop(duplicate);
        assert!(StateLock::acquire(dir.file()).is_err());
        drop(next);
        assert!(StateLock::acquire(dir.file()).is_ok());
    }

    #[test]
    fn process_owner_helper() {
        let Some(dir) = std::env::var_os("FREYA_LOCK_TEST_DIRECTORY") else { return; };
        let dir = PathBuf::from(dir);
        let file = OpenOptions::new().read(true).write(true).open(dir.join("writer.lock")).unwrap();
        let _owner = StateLock::acquire(file).unwrap();
        fs::write(dir.join("ready"), b"locked").unwrap();
        while !dir.join("release").exists() { thread::sleep(Duration::from_millis(5)); }
    }

    #[test]
    fn process_exit_and_kill_release_ownership_preserving_checkpoint() {
        let helper = format!("{}::process_owner_helper", module_path!().split_once("::").unwrap().1);
        for kill in [false, true] {
            let dir = Fixture::new();
            drop(dir.file());
            let checkpoint = b"unresolved claims must survive";
            fs::write(dir.0.join("checkpoint.json"), checkpoint).unwrap();
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", &helper, "--nocapture"])
                .env("FREYA_LOCK_TEST_DIRECTORY", &dir.0).spawn().unwrap();
            let until = Instant::now() + Duration::from_secs(5);
            while !dir.0.join("ready").exists() && Instant::now() < until {
                if let Some(status) = child.try_wait().unwrap() { panic!("helper exited early: {status}"); }
                thread::sleep(Duration::from_millis(5));
            }
            if !dir.0.join("ready").exists() {
                let _ = child.kill(); let _ = child.wait(); panic!("lock helper timed out");
            }
            assert!(StateLock::acquire(dir.file()).is_err());
            if kill { child.kill().unwrap(); }
            else { fs::write(dir.0.join("release"), b"release").unwrap(); }
            let status = child.wait().unwrap();
            assert_eq!(status.success(), !kill);
            let _next = StateLock::acquire(dir.file()).unwrap();
            assert_eq!(fs::read(dir.0.join("checkpoint.json")).unwrap(), checkpoint);
        }
    }
}
