<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="art/letterhead-ondark.svg">
    <source media="(prefers-color-scheme: light)" srcset="art/letterhead-onwhite.svg">
    <img alt="Spaces" src="art/letterhead-onwhite.svg" width="750">
  </picture>
</p>

# Spaces
Spaces provide a chroot-like sandboxing environment for you to access your favorite distributions: Arch, Fedora, Kali, and Ubuntu. A simple permission system ensures your local files and credentials remain secure, even if your space is compromised. Spaces are constructed directly using packages from your chosen distribution repositories with signature enforcement. No container middleman or surprises.

## About

Spaces is a "simple" wrapper around `systemd-nspawn` that makes it easier to use and provides host integration with a couple of intuitive permissions, dbus/theming integration, and PAM.

To use, type in your terminal:

```bash
spaces enter ubuntu # or fedora, arch, or kali
```

And follow the graphical prompts. After the space initializes, you will face a familiar terminal. Except, this time, it is a real Ubuntu system you can do anything you want in. Install packages, add custom services, use docker, install vs code, your dev toolchains, browsers, etc.

Everything works as it would on a normal system. If you install desktop packages, they appear on your taskbar. If you share your screen using Chrome, it works. The only overhead is 75mb of memory and 3 seconds of booting.

If you want your space to run on boot, run:
```bash
sudo systemctl enable --now spaces@<your-space>
```

And it will start on boot. You may stop that service to poweroff your space. Once started, such as by launching applications, the service or entering a space, the space does not power down until you shutdown your computer/server. Idle detection may be added in the future.

However, if your space relies on system services accessing your user data, this will not work, as your user and its files will not be mounted on boot.  This is done partly for security reasons, and partly to avoid edge cases with solutions such as systemd-homed.

Alternatively, you may use the user service. This will start the space when you login, so that the first application you launch starts faster. As you are logged in, your user will be mounted when the space starts, eliminating timing issues with those system services. Once started, the space will remain active until poweroff, even if you log out.
```bash
systemctl --user enable --now spaces@<your-space>
```

You can also enable lingering for your user, which starts the user manager at
boot without requiring an interactive login.
```bash
loginctl enable-linger
```

## Security
For security issues, email: security@anatase.org

Spaces is a small daemon that escalates using polkits. The root inside spaces is powerful depending on granted permissions, and requires the same excalation pattern as the host. This means: to get root in a space, the same authentication that would be performed on the host is required and unpriviledged user processes need a normal bypass. One action is allowed without authentication: a configured user chooses to enter a space as themselves. During this escalation, the service `spaces@<space>` is also started if needed. This is ok, because the permissions and users that have been granted to a space previously used authentication, and all rootful applications added to that space also used PAM authentication for e.g., sudo. Therefore, just turning on a space does not give unpriviledged code an escalation path, regardless of whether that code is inside or outside the space.

Spaces only mounts users that have executed `spaces configure --user <space>` or were the ones to create the space and only after they log in. The root inside spaces is the same root as the host. This was chosen because user namespaces cannot do certain actions such as modify the host network (would need a bridge and would not be able to open ports) or change kernel settings. Even though the default priviledge level of spaces disallows these actions, a key requirement in design was making spaces able to change permissions without recreating them. However, this also means that spaces can be victim to a class of security issues such as improper mounts, kernel bugs, or other sandboxing holes that can cause the root inside the space to escape.

It is not possible to mount SSH or GPG directories from the host into the space. This is blocked through both not supporting mounting the whole home directory and with SELinux as a second layer (if your host supports it). Only agent forwarding is supported.

## Contributing

Spaces does not currently accept external contributions. You are welcome to post issues in the issue tracker, with suggestions or bug reports.

## License

A copy of Spaces is provided to you under the terms of [GNU Affero General Public License v3.0 or later](LICENSE).

The files under `./art/distros` and `./src/spaces/overlay` are used as identifiers to their respective distributions with no implication of association or affiliation to their respective communities or companies.
