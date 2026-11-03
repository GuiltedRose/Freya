//! Bundled headless entrypoint. Existing Rust I/O and Python evidence ownership
//! are reused; this executable is not a consensus or transaction-execution node.
#![forbid(unsafe_code)]
use std::{env,process::{Command,ExitCode}};
fn main()->ExitCode {
    if env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("--peer")) {
        let Some(engine) = env::var_os("FREYA_NODE_PEER") else { return ExitCode::FAILURE; };
        let mut command = Command::new(engine);
        command.args(env::args_os().skip(2));
        for key in ["SOLANA_PRIVATE_KEY", "SOLANA_KEYPAIR_PATH", "SOLANA_ENV_FILE"] { command.env_remove(key); }
        #[cfg(unix)] {
            use std::os::unix::process::CommandExt;
            let _ = command.exec();
            return ExitCode::FAILURE;
        }
        #[cfg(not(unix))] { return ExitCode::FAILURE; }
    }
    let python=match env::var_os("FREYA_NODE_PYTHON") {Some(p)=>p,None=>{eprintln!("Use the packaged freya-node entrypoint");return ExitCode::FAILURE;}};
    let worker=match env::var_os("FREYA_NODE_WORKER") {Some(p)=>p,None=>{eprintln!("Missing packaged worker");return ExitCode::FAILURE;}};
    let mut command=Command::new(python);
    command.arg(worker).args(env::args_os().skip(1));
    for key in ["SOLANA_PRIVATE_KEY","SOLANA_KEYPAIR_PATH","SOLANA_ENV_FILE"] {command.env_remove(key);}
    #[cfg(unix)] {
        use std::os::unix::process::CommandExt;
        let _=command.exec();
        eprintln!("Unable to start packaged worker; details omitted");
        ExitCode::FAILURE
    }
    #[cfg(not(unix))] {match command.status(){Ok(s) if s.success()=>ExitCode::SUCCESS,_=>ExitCode::FAILURE}}
}
