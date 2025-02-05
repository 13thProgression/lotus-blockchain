import keyring as keyring_main

from lotus.util.default_root import DEFAULT_KEYS_ROOT_PATH
from lotus.util.file_keyring import FileKeyring
from lotus.util.misc import prompt_yes_no
from keyrings.cryptfile.cryptfile import CryptFileKeyring  # pyright: reportMissingImports=false
from pathlib import Path
from sys import exit, platform
from typing import Any, Optional, Tuple, Union


# We want to protect the keyring, even if a user-specified master passphrase isn't provided
DEFAULT_PASSPHRASE_IF_NO_MASTER_PASSPHRASE = "$ lotus passphrase set # all the cool kids are doing it!"


class KeyringWrapper:
    """
    KeyringWrapper provides an abstraction that the Keychain class can use
    without requiring knowledge of the keyring backend. During initialization,
    a keyring backend is selected based on the OS.

    The wrapper is implemented as a singleton, as it may need to manage state
    related to the master passphrase and handle migration from the legacy
    CryptFileKeyring implementation.
    """

    # Static members
    __shared_instance = None
    __keys_root_path: Path = DEFAULT_KEYS_ROOT_PATH

    # Instance members
    keys_root_path: Path
    keyring: Union[Any, FileKeyring] = None
    cached_passphase: Optional[str] = DEFAULT_PASSPHRASE_IF_NO_MASTER_PASSPHRASE
    cached_passphase_is_validated: bool = False
    legacy_keyring = None

    def __init__(self, keys_root_path: Path = DEFAULT_KEYS_ROOT_PATH):
        """
        Initializes the keyring backend based on the OS. For Linux, we previously
        used CryptFileKeyring. We now use our own FileKeyring backend and migrate
        the data from the legacy CryptFileKeyring (on write).
        """
        self.keys_root_path = keys_root_path
        self.keyring = self._configure_backend()

        # Configure the legacy keyring if keyring passphrases are supported to support migration (if necessary)
        self.legacy_keyring = self._configure_legacy_backend()

    def _configure_backend(self) -> Union[Any, FileKeyring]:
        from lotus.util.keychain import supports_keyring_passphrase

        if self.keyring:
            raise Exception("KeyringWrapper has already been instantiated")

        if platform == "win32" or platform == "cygwin":
            import keyring.backends.Windows

            keyring.set_keyring(keyring.backends.Windows.WinVaultKeyring())
        elif platform == "darwin":
            import keyring.backends.macOS

            keyring.set_keyring(keyring.backends.macOS.Keyring())
            # TODO: New keyring + passphrase support can be enabled for macOS by updating
            # supports_keyring_passphrase() and uncommenting the lines below. Leaving the
            # lines below in place for testing.
            #
            # if supports_keyring_passphrase():
            #     keyring = FileKeyring(keys_root_path=self.keys_root_path)  # type: ignore
            # else:
            #     keyring.set_keyring(keyring.backends.macOS.Keyring())
        elif platform == "linux":
            if supports_keyring_passphrase():
                keyring = FileKeyring(keys_root_path=self.keys_root_path)  # type: ignore
            else:
                keyring = CryptFileKeyring()
                keyring.keyring_key = "your keyring password"  # type: ignore
        else:
            keyring = keyring_main

        return keyring

    def _configure_legacy_backend(self) -> CryptFileKeyring:
        # If keyring.yaml isn't found or is empty, check if we're using CryptFileKeyring
        filekeyring = self.keyring if type(self.keyring) == FileKeyring else None
        if filekeyring and not filekeyring.has_content():
            old_keyring = CryptFileKeyring()
            if Path(old_keyring.file_path).is_file():
                # After migrating content from legacy_keyring, we'll prompt to clear those keys
                old_keyring.keyring_key = "your keyring password"  # type: ignore
                return old_keyring

        return None

    @staticmethod
    def set_keys_root_path(keys_root_path: Path):
        """
        Used to set the keys_root_path prior to instantiating the __shared_instance
        """
        KeyringWrapper.__keys_root_path = keys_root_path

    @staticmethod
    def get_shared_instance(create_if_necessary=True):
        if not KeyringWrapper.__shared_instance and create_if_necessary:
            KeyringWrapper.__shared_instance = KeyringWrapper(keys_root_path=KeyringWrapper.__keys_root_path)

        return KeyringWrapper.__shared_instance

    @staticmethod
    def cleanup_shared_instance():
        KeyringWrapper.__shared_instance = None

    def get_keyring(self):
        """
        Return the current keyring backend. The legacy keyring is preferred if it's in use
        """
        return self.keyring if not self.using_legacy_keyring() else self.legacy_keyring

    def using_legacy_keyring(self) -> bool:
        return self.legacy_keyring is not None

    # Master passphrase support

    def keyring_supports_master_passphrase(self) -> bool:
        return type(self.get_keyring()) in [FileKeyring]

    def get_cached_master_passphrase(self) -> Tuple[Optional[str], bool]:
        """
        Returns a tuple including the currently cached passphrase and a bool
        indicating whether the passphrase has been previously validated.
        """
        return self.cached_passphase, self.cached_passphase_is_validated

    def set_cached_master_passphrase(self, passphrase: Optional[str], validated=False) -> None:
        """
        Cache the provided passphrase and optionally indicate whether the passphrase
        has been validated.
        """
        self.cached_passphase = passphrase
        self.cached_passphase_is_validated = validated

    def has_cached_master_passphrase(self) -> bool:
        passphrase = self.get_cached_master_passphrase()
        return passphrase is not None and len(passphrase) > 0

    def has_master_passphrase(self) -> bool:
        """
        Returns a bool indicating whether the underlying keyring data
        is secured by a master passphrase.
        """
        return self.keyring_supports_master_passphrase() and self.keyring.has_content()

    def master_passphrase_is_valid(self, passphrase: str, force_reload: bool = False) -> bool:
        return self.keyring.check_passphrase(passphrase, force_reload=force_reload)

    def set_master_passphrase(
        self,
        current_passphrase: Optional[str],
        new_passphrase: str,
        write_to_keyring: bool = True,
        allow_migration: bool = True,
    ) -> None:
        """
        Sets a new master passphrase for the keyring
        """

        from lotus.util.keychain import KeyringCurrentPassphaseIsInvalid, KeyringRequiresMigration

        # Require a valid current_passphrase
        if (
            self.has_master_passphrase()
            and current_passphrase is not None
            and not self.master_passphrase_is_valid(current_passphrase)
        ):
            raise KeyringCurrentPassphaseIsInvalid("invalid current passphrase")

        self.set_cached_master_passphrase(new_passphrase, validated=True)

        if write_to_keyring:
            # We'll migrate the legacy contents to the new keyring at this point
            if self.using_legacy_keyring():
                if not allow_migration:
                    raise KeyringRequiresMigration("keyring requires migration")

                self.migrate_legacy_keyring()
            else:
                # We're reencrypting the keyring contents using the new passphrase. Ensure that the
                # payload has been decrypted by calling load_keyring with the current passphrase.
                self.keyring.load_keyring(passphrase=current_passphrase)
                self.keyring.write_keyring(fresh_salt=True)  # Create a new salt since we're changing the passphrase

    def remove_master_passphrase(self, current_passphrase: Optional[str]) -> None:
        """
        Remove the user-specific master passphrase. We still keep the keyring contents encrypted
        using the default passphrase.
        """
        self.set_master_passphrase(current_passphrase, DEFAULT_PASSPHRASE_IF_NO_MASTER_PASSPHRASE)

    # Legacy keyring migration

    def confirm_migration(self) -> bool:
        """
        Before beginning migration, we'll notify the user that the legacy keyring needs to be
        migrated and warn about backing up the mnemonic seeds.

        If a master passphrase hasn't been explicitly set yet, we'll attempt to prompt and set
        the passphrase prior to beginning migration.
        """

        master_passphrase, _ = self.get_cached_master_passphrase()
        if master_passphrase == DEFAULT_PASSPHRASE_IF_NO_MASTER_PASSPHRASE:
            print(
                "\nYour existing keys need to be migrated to a new keyring that is optionally secured by a master "
                "passphrase."
            )
            print(
                "Would you like to set a master passphrase now? Use 'lotus passphrase set' to change the passphrase.\n"
            )

            response = prompt_yes_no("Set keyring master passphrase? (y/n) ")
            if response:
                from lotus.cmds.passphrase_funcs import prompt_for_new_passphrase

                # Prompt for a master passphrase and cache it
                new_passphrase = prompt_for_new_passphrase()
                self.set_master_passphrase(
                    current_passphrase=None, new_passphrase=new_passphrase, write_to_keyring=False
                )
            else:
                print(
                    "Will skip setting a master passphrase. Use 'lotus passphrase set' to set the master passphrase.\n"
                )
        else:
            import colorama

            colorama.init()

            print("\nYour existing keys will be migrated to a new keyring that is secured by your master passphrase")
            print(colorama.Fore.YELLOW + colorama.Style.BRIGHT + "WARNING: " + colorama.Style.RESET_ALL, end="")
            print(
                "It is strongly recommended that you ensure you have a copy of the mnemonic seed for each of your "
                "keys prior to beginning migration\n"
            )

        return prompt_yes_no("Begin keyring migration? (y/n) ")

    def migrate_legacy_keyring(self):
        """
        Handle importing keys from the legacy keyring into the new keyring.

        Prior to beginning, we'll ensure that we at least suggest setting a master passphrase
        and backing up mnemonic seeds. After importing keys from the legacy keyring, we'll
        perform a before/after comparison of the keyring contents, and on success we'll prompt
        to cleanup the legacy keyring.
        """

        from lotus.util.keychain import Keychain, MAX_KEYS

        # Make sure the user is ready to begin migration. We want to ensure that
        response = self.confirm_migration()
        if not response:
            print("Skipping migration. Unable to proceed")
            exit(0)

        print("Migrating contents from legacy keyring")

        keychain = Keychain()
        # Obtain contents from the legacy keyring. When using the Keychain interface
        # to read, the legacy keyring will be preferred over the new keyring.
        original_private_keys = keychain.get_all_private_keys()
        service = keychain._get_service()
        user_passphrase_pairs = []
        index = 0
        user = keychain._get_private_key_user(index)
        while index <= MAX_KEYS:
            # Build up a list of user/passphrase tuples from the legacy keyring contents
            if user is not None:
                passphrase = self.get_passphrase(service, user)

            if passphrase is not None:
                user_passphrase_pairs.append((user, passphrase))

            index += 1
            user = keychain._get_private_key_user(index)

        # Write the keys directly to the new keyring (self.keyring)
        for (user, passphrase) in user_passphrase_pairs:
            self.keyring.set_password(service, user, passphrase)

        # Stop using the legacy keyring. This will direct subsequent reads to the new keyring.
        old_keyring = self.legacy_keyring
        self.legacy_keyring = None

        print("Verifying migration results...", end="")

        # Compare the original keyring contents with the new
        try:
            post_migration_private_keys = keychain.get_all_private_keys()

            if post_migration_private_keys == original_private_keys:
                print(" Verified")
        except Exception as e:
            print(f"\nMigration failed: {e}")
            print("Leaving legacy keyring intact")
            exit(1)

        print(f"Keyring migration completed successfully ({str(self.keyring.keyring_path)})\n")

        # Ask if we should clean up the legacy keyring
        self.confirm_legacy_keyring_cleanup(old_keyring, service, [user for (user, _) in user_passphrase_pairs])

    def confirm_legacy_keyring_cleanup(self, legacy_keyring, service, users):
        """
        Ask the user whether we should remove keys from the legacy keyring. We can't just
        delete the file because other python processes might use the same keyring file.
        """

        response = prompt_yes_no(f"Remove keys from old keyring ({str(legacy_keyring.file_path)})? (y/n) ")

        if response:
            for user in users:
                legacy_keyring.delete_password(service, user)
            print("Removed keys from old keyring")
        else:
            print("Keys in old keyring left intact")

        # TODO: CryptFileKeyring doesn't cleanup section headers
        # [lotus_2Duser_2Dlotus_2D1_2E8] is left behind

    # Keyring interface

    def get_passphrase(self, service: str, user: str) -> str:
        # Continue reading from the legacy keyring until we want to write something,
        # at which point we'll migrate the legacy contents to the new keyring
        if self.using_legacy_keyring():
            return self.legacy_keyring.get_password(service, user)  # type: ignore

        return self.get_keyring().get_password(service, user)

    def set_passphrase(self, service: str, user: str, passphrase: str):
        # On the first write while using the legacy keyring, we'll start migration
        if self.using_legacy_keyring() and self.has_cached_master_passphrase():
            self.migrate_legacy_keyring()

        self.get_keyring().set_password(service, user, passphrase)

    def delete_passphrase(self, service: str, user: str):
        # On the first write while using the legacy keyring, we'll start migration
        if self.using_legacy_keyring() and self.has_cached_master_passphrase():
            self.migrate_legacy_keyring()

        self.get_keyring().delete_password(service, user)
