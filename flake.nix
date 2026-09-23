{
  description = "Switch between Claude Code and Codex subscription logins";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          agent-switcher = pkgs.stdenvNoCC.mkDerivation {
            pname = "agent-switcher";
            version = "0.4.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            buildInputs = [ pkgs.python3 ];
            doCheck = true;
            checkPhase = "${pkgs.python3}/bin/python3 -m unittest -v test_agent_switch";
            # macOS ships pgrep and security in /usr/bin
            pathPrefix = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.procps ]}";
            installPhase = ''
              install -Dm755 agent_switch.py $out/libexec/agent_switch.py
            '';
            postFixup = ''
              for tool in claude codex; do
                makeWrapper $out/libexec/agent_switch.py $out/bin/$tool-switch \
                  --add-flags "--tool $tool" $pathPrefix
              done
            '';
          };
          claude-switch = self.packages.${system}.agent-switcher;
          default = self.packages.${system}.agent-switcher;
        }
      );
    };
}
