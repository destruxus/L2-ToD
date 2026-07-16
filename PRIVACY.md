# Privacy Policy — L2-ToD Bot

**Last updated: 16 July 2026**

This policy explains what data the L2-ToD Discord bot ("the Bot") stores and why. The Bot is designed to store the minimum data necessary to operate.

## 1. Data We Store

When a server administrator configures the Bot, the following is stored in the Bot's database:

- **Discord Server ID** — to keep each server's data isolated
- **Configured Channel IDs** — the channels where the Bot posts alerts and the live timer overview
- **Configured Role ID (optional)** — if an administrator restricts timer commands to a role
- **Boss timer state** — boss identifier, time of death, window start/end times, and timer status
- **Custom boss definitions** — names, respawn hours, and window durations added by administrators
- **Discord User ID and display name of command users** — shown in timer messages to indicate who set or adjusted a timer

## 2. Data We Do NOT Store

- Message content (outside of the answers given during the configuration conversation, which are processed and not retained as raw messages)
- Email addresses, IP addresses, or any personal information beyond Discord identifiers
- Data from servers where the Bot is not present

## 3. How Data Is Used

Stored data is used solely to operate the Bot's timer features. It is never sold, shared with third parties, or used for advertising or analytics.

## 4. Data Retention & Deletion

Data is retained while the Bot is a member of your server. A server administrator can permanently delete all data associated with their server at any time by running the `/wipe_my_data` command. Deletion is immediate and irreversible. In addition, if the Bot is removed from a server or permanently loses access to its configured channels, that server's data is deleted automatically after a 7-day grace period.

## 5. Data Storage & Security

Data is stored in a database on a private server operated by the Bot's maintainer. Access is limited to the Bot process and the maintainer.

## 6. Third Parties

The Bot operates on the Discord platform; your use of Discord is governed by Discord's own Privacy Policy. The Bot does not transmit your data to any other third party.

## 7. Children's Privacy

The Bot does not knowingly collect data from anyone under the minimum age required by Discord's Terms of Service in their jurisdiction.

## 8. Changes to This Policy

This policy may be updated from time to time. The "last updated" date at the top reflects the latest revision.

## 9. Contact

Questions or data-related requests can be raised via the project's GitHub repository issue tracker.
