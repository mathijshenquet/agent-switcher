{
  description = "Switch between Claude Code subscription logins";

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
          claude-switch = pkgs.stdenvNoCC.mkDerivation {
            pname = "claude-switch";
            version = "0.3.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            buildInputs = [ pkgs.python3 ];
            doCheck = true;
            checkPhase = "${pkgs.python3}/bin/python3 -m unittest -v test_claude_switch";
            installPhase = ''
              install -Dm755 claude_switch.py $out/bin/claude-switch
            '';
            # macOS ships pgrep and security in /usr/bin
            postFixup = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux ''
              wrapProgram $out/bin/claude-switch --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.procps ]}
            '';
          };
          default = self.packages.${system}.claude-switch;
        }
      );
    };
}
