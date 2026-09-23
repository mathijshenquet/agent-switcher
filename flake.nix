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
          claude-switch = pkgs.writeShellApplication {
            name = "claude-switch";
            runtimeInputs = [ pkgs.jq ] ++ pkgs.lib.optional pkgs.stdenv.hostPlatform.isLinux pkgs.procps;
            # jq filters are single-quoted on purpose
            excludeShellChecks = [ "SC2016" ];
            text = builtins.readFile ./claude-switch;
          };
          default = self.packages.${system}.claude-switch;
        }
      );
    };
}
