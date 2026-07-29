use plist::{Dictionary, Value};
use serde::Serialize;
use std::{
    collections::HashMap,
    env, fs,
    path::{Path, PathBuf},
};
use url::Url;

pub const DEFAULT_PORT: u16 = 8443;

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct RuntimeConfig {
    pub workspace: Option<PathBuf>,
    pub runtime_root: PathBuf,
    pub port: u16,
    pub server_url: Url,
    pub server_plist: PathBuf,
    pub engine_program: Option<PathBuf>,
}

fn dotenv(path: &Path) -> HashMap<String, String> {
    let Ok(text) = fs::read_to_string(path) else {
        return HashMap::new();
    };
    text.lines()
        .filter_map(|raw| {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let (key, value) = line.split_once('=')?;
            let value = value.trim().trim_matches(|c| c == '"' || c == '\'');
            Some((key.trim().to_string(), value.to_string()))
        })
        .collect()
}

fn string_value(dictionary: &Dictionary, key: &str) -> Option<String> {
    dictionary
        .get(key)
        .and_then(Value::as_string)
        .map(str::to_owned)
}

fn parse_port(raw: Option<&str>) -> u16 {
    raw.and_then(|value| value.parse::<u16>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_PORT)
}

pub fn discover(home: &Path, environment: &HashMap<String, String>) -> RuntimeConfig {
    let server_plist = home.join("Library/LaunchAgents/com.ciao.server.plist");
    let plist = Value::from_file(&server_plist)
        .ok()
        .and_then(|value| value.into_dictionary())
        .unwrap_or_default();
    let launch_env = plist
        .get("EnvironmentVariables")
        .and_then(Value::as_dictionary)
        .cloned()
        .unwrap_or_default();

    let workspace = string_value(&launch_env, "CIAO_WORKSPACE")
        .or_else(|| string_value(&plist, "WorkingDirectory"))
        .or_else(|| environment.get("CIAO_WORKSPACE").cloned())
        .filter(|value| !value.trim().is_empty())
        .map(|value| {
            PathBuf::from(&value)
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from(value))
        });
    let workspace_env = workspace
        .as_ref()
        .map(|path| dotenv(&path.join(".env")))
        .unwrap_or_default();

    let override_url = environment
        .get("CIAO_DESKTOP_SERVER_URL")
        .and_then(|value| Url::parse(value).ok());
    let plist_port = string_value(&launch_env, "CIAO_PORT");
    let configured_port = workspace_env
        .get("PWA_PORT")
        .map(String::as_str)
        .or(plist_port.as_deref())
        .or_else(|| environment.get("CIAO_PORT").map(String::as_str));
    let port = override_url
        .as_ref()
        .and_then(Url::port_or_known_default)
        .unwrap_or_else(|| parse_port(configured_port));
    let server_url =
        override_url.unwrap_or_else(|| Url::parse(&format!("http://localhost:{port}/")).unwrap());

    let runtime_root = workspace_env
        .get("CIAO_RUNTIME_ROOT")
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
        .map(|path| {
            if path.is_absolute() {
                path
            } else {
                workspace
                    .as_ref()
                    .map_or(path.clone(), |root| root.join(path))
            }
        })
        .or_else(|| workspace.as_ref().map(|path| path.join(".runtime")))
        .unwrap_or_else(|| home.join(".ciaobot/runtime"));

    let engine_program = plist
        .get("ProgramArguments")
        .and_then(Value::as_array)
        .and_then(|values| values.first())
        .and_then(Value::as_string)
        .map(PathBuf::from);

    RuntimeConfig {
        workspace,
        runtime_root,
        port,
        server_url,
        server_plist,
        engine_program,
    }
}

pub fn discover_current() -> RuntimeConfig {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/"));
    discover(&home, &env::vars().collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use plist::Value;
    use tempfile::tempdir;

    #[test]
    fn dotenv_overrides_plist_port_and_resolves_relative_runtime() {
        let temp = tempdir().unwrap();
        let home = temp.path();
        let workspace = home.join("workspace");
        let agents = home.join("Library/LaunchAgents");
        fs::create_dir_all(&workspace).unwrap();
        fs::create_dir_all(&agents).unwrap();
        fs::write(
            workspace.join(".env"),
            "PWA_PORT=9555\nCIAO_RUNTIME_ROOT=var/runtime\n",
        )
        .unwrap();
        let mut launch_env = Dictionary::new();
        launch_env.insert("CIAO_PORT".into(), Value::String("8443".into()));
        let mut root = Dictionary::new();
        root.insert(
            "WorkingDirectory".into(),
            Value::String(workspace.to_string_lossy().into_owned()),
        );
        root.insert("EnvironmentVariables".into(), Value::Dictionary(launch_env));
        Value::Dictionary(root)
            .to_file_xml(agents.join("com.ciao.server.plist"))
            .unwrap();

        let config = discover(home, &HashMap::new());

        assert_eq!(config.port, 9555);
        assert_eq!(
            config.runtime_root,
            workspace.canonicalize().unwrap().join("var/runtime")
        );
        assert_eq!(config.server_url.as_str(), "http://localhost:9555/");
    }

    #[test]
    fn development_url_override_wins() {
        let temp = tempdir().unwrap();
        let environment = HashMap::from([(
            "CIAO_DESKTOP_SERVER_URL".to_string(),
            "http://localhost:9777/base".to_string(),
        )]);
        let config = discover(temp.path(), &environment);
        assert_eq!(config.port, 9777);
        assert_eq!(config.server_url.as_str(), "http://localhost:9777/base");
    }
}
