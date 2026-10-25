//! Trust only the socket peer or a Cloudflare header on a loopback-only
//! origin.

use std::net::IpAddr;

use axum::http::HeaderMap;

pub fn capture(
    path: &str,
    peer: IpAddr,
    cloudflare: bool,
    headers: &HeaderMap,
) -> Option<([u8; 64], IpAddr)> {
    let token = path.strip_prefix("/email/open/")?.strip_suffix(".gif")?;
    let token: [u8; 64] = token.as_bytes().try_into().ok()?;
    if !token
        .iter()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    {
        return None;
    }
    let ip = if cloudflare {
        if !peer.is_loopback() {
            return None;
        }
        let mut supplied = headers.get_all("cf-connecting-ip").iter();
        let ip = supplied.next()?.to_str().ok()?.parse().ok()?;
        if supplied.next().is_some() {
            return None;
        }
        ip
    } else {
        peer
    };
    Some((token, ip))
}

#[cfg(test)]
mod tests {
    use axum::http::{HeaderMap, HeaderValue};

    use super::capture;

    #[test]
    fn direct_capture_uses_a_fixed_token_and_ignores_spoofed_forwarding_headers()
    -> anyhow::Result<()> {
        let path = format!("/email/open/{}.gif", "a".repeat(64));
        let mut headers = HeaderMap::new();
        headers.insert(
            "x-forwarded-for",
            HeaderValue::from_static("203.0.113.9"),
        );
        headers.insert(
            "cf-connecting-ip",
            HeaderValue::from_static("203.0.113.8"),
        );
        let peer = "127.0.0.1".parse()?;
        let (_, ip) = capture(&path, peer, false, &headers)
            .ok_or_else(|| anyhow::anyhow!("Missing opening signal"))?;
        assert_eq!(ip, peer);
        assert!(
            capture("/email/open/invalid.gif", peer, false, &headers)
                .is_none()
        );
        Ok(())
    }

    #[test]
    fn cloudflare_capture_requires_a_loopback_peer_and_one_valid_address()
    -> anyhow::Result<()> {
        let path = format!("/email/open/{}.gif", "a".repeat(64));
        let loopback = "127.0.0.1".parse()?;
        let mut headers = HeaderMap::new();
        assert!(capture(&path, loopback, true, &headers).is_none());
        headers.insert(
            "cf-connecting-ip",
            HeaderValue::from_static("2001:db8::7"),
        );
        let (_, ip) = capture(&path, loopback, true, &headers)
            .ok_or_else(|| anyhow::anyhow!("Missing opening signal"))?;
        assert_eq!(ip.to_string(), "2001:db8::7");
        assert!(
            capture(&path, "203.0.113.2".parse()?, true, &headers).is_none()
        );
        headers.append(
            "cf-connecting-ip",
            HeaderValue::from_static("203.0.113.7"),
        );
        assert!(capture(&path, loopback, true, &headers).is_none());
        Ok(())
    }
}
