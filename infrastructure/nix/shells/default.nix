{pkgs}:
pkgs.mkShell {
  packages = with pkgs; [
    bazelisk
    gcc
    gnumake
    pkg-config
    rust-bin.stable.latest.default
    cargo-watch
    cargo-nextest
    nodejs_24 # nodejs_26 does not work.
    pnpm
    python313
    uv
    docker-compose
    docker-client
    git
    jq
    yq-go
    tree
    ripgrep
    fd
    alejandra
  ];

  shellHook =
    if pkgs.stdenv.isDarwin
    then ''
      export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
      export SDKROOT=$(xcrun --show-sdk-path)
      export CC=/usr/bin/clang
      export CXX=/usr/bin/clang++
    ''
    else "";
}
