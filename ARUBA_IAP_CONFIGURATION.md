# Guide de Configuration Aruba Instant AP (IAP) - Wi-Fi Invités MPSK

Ce document détaille l'ensemble des configurations à appliquer sur votre contrôleur virtuel **Aruba Instant AP** (séries IAP-305, IAP-505, etc.) pour déployer l'authentification **MPSK Local** (clé Wi-Fi unique par invité) avec **contrôle matériel de bande passante** par rôle.

Toutes les informations ci-dessous sont **anonymisées** et prêtes à être adaptées à votre plan d'adressage réseau.

---

## 1. Vue d'Ensemble de l'Architecture

```text
[ Smartphone / PC Invité ]
           |  (Scan QR Code -> WPA2/WPA3 Personnel avec clé unique)
           v
[ Borne Aruba Instant AP ]
   ├── SSID : <SSID_NAME> (opmode: mpsk-local)
   ├── Profil MPSK : MPSK_GUEST
   └── Table de Routage Datapath (ASIC BWM)
        ├── Rôle "Guest-Standard"   -> Bridage à 10 Mbps (Per-User) -> VLAN <VLAN_GUEST_ID>
        ├── Rôle "Guest-Streaming"  -> Bridage à 50 Mbps (Per-User) -> VLAN <VLAN_GUEST_ID>
        └── Rôle "Guest-VIP"        -> Débit maximal Illimité       -> VLAN <VLAN_GUEST_ID>
```

---

## 2. Prérequis

1. **Borne Aruba Instant AP** sous **ArubaOS 8.x** ou supérieur.
2. **Accès SSH activé** sur l'IP du contrôleur virtuel Instant AP.
3. **VLAN Invités dédié** configuré sur votre commutateur/routeur avec un serveur DHCP actif et un accès Internet sans accès aux sous-réseaux privés internes (RFC1918).

---

## 3. Configuration en Ligne de Commande (CLI SSH)

Connectez-vous en SSH sur le contrôleur virtuel :

```bash
ssh admin@<IP_VIRTUELLE_CONTROLEUR_AP>
```

Passez en mode configuration et appliquez les blocs suivants :

```text
conf t

! =============================================================================
! 1. Profil de stockage des clés MPSK locales
! =============================================================================
wlan mpsk-local MPSK_GUEST

! =============================================================================
! 2. Rôles et Règles de Bande Passante (Contrats matériels ASIC)
! =============================================================================

! Profil Invité Standard (10 Mbps Download / 10 Mbps Upload individuel)
wlan access-rule Guest-Standard
 vlan <VLAN_GUEST_ID>
 bandwidth-limit peruser downstream 10000
 bandwidth-limit peruser upstream 10000
 rule any any match any any any permit

! Profil Streaming / Télétravail (50 Mbps Download / 50 Mbps Upload individuel)
wlan access-rule Guest-Streaming
 vlan <VLAN_GUEST_ID>
 bandwidth-limit peruser downstream 50000
 bandwidth-limit peruser upstream 50000
 rule any any match any any any permit

! Profil VIP / Direction (Bande passante maximale sans restriction)
wlan access-rule Guest-VIP
 vlan <VLAN_GUEST_ID>
 rule any any match any any any permit

! =============================================================================
! 3. Création et association du SSID Wi-Fi Invités
! =============================================================================
wlan ssid-profile <SSID_NAME>
 enable
 type guest
 essid <SSID_NAME>
 opmode mpsk-local
 mpsk-local MPSK_GUEST
 vlan <VLAN_GUEST_ID>
 broadcast-filter all
 dtim-period 1
 inact-def-timeout 0
 max-authentication-failures 0

end

! =============================================================================
! 4. Enregistrement en mémoire Flash
! =============================================================================
commit apply
```

> **Note importante** : La commande `bandwidth-limit peruser` configure un contrat de bande passante alloué **séparément à chaque client connecté**, sans partager le quota entre plusieurs invités.
> **Note sur le profil MPSK** : Le nom défini dans `wlan mpsk-local <MPSK_PROFILE>` et référencé dans le SSID via `mpsk-local <MPSK_PROFILE>` **doit correspondre exactement** à la variable `ARUBA_MPSK_PROFILE` dans votre configuration (ex: `Guest-MPSK` ou `MPSK_GUEST`).

---

## 4. Liaison avec l'Application Web (`.env` & Docker)

Dans le fichier `.env` de l'application Wi-Fi Invité, renseignez les paramètres correspondants :

```dotenv
# Configuration Aruba Instant AP
ARUBA_MODE=instant
ARUBA_INSTANT_HOST=https://<IP_VIRTUELLE_CONTROLEUR_AP>:4343
ARUBA_INSTANT_USERNAME=admin
ARUBA_INSTANT_PASSWORD=<VOTRE_MOT_DE_PASSE_ADMIN_AP>
ARUBA_INSTANT_VERIFY_SSL=false
ARUBA_MPSK_PROFILE=Guest-MPSK

# SSID et Paramètres par défaut
WIFI_SSID=<SSID_NAME>
ADMIN_PASSWORD=<VOTRE_MOT_DE_PASSE_PORTAIL_ADMIN>
```

> [!IMPORTANT]
> **Déploiement Docker (`docker-compose.yml`)** :
> 1. Montez impérativement le fichier `profiles.json` en volume (`- ./profiles.json:/app/profiles.json`) pour conserver les profils personnalisés entre les redémarrages de conteneur.
> 2. Si votre borne Aruba se trouve sur un sous-réseau routé ou un VLAN dédié (ex: `10.10.30.0/24`), assurez-vous que le conteneur Docker a bien accès à cette plage IP. Si le réseau Docker Bridge `172.x` ne route pas vers l'IP de l'AP, utilisez `network_mode: host` dans `docker-compose.yml`.

L'application interagira automatiquement avec la borne via SSH pour :
- Ajouter dynamiquement les clés avec leur rôle de vitesse lors de la création d'un invité :  
  `mpsk-local-passphrase <guest_id> <password> <role_name>`
- Supprimer automatiquement les clés arrivées à expiration :  
  `no mpsk-local-passphrase <guest_id>`
- Créer ou modifier à chaud les règles de bande passante (`wlan access-rule`) et les contrats Datapath ASIC (`bandwidth-limit peruser`) depuis l'interface web Admin.
- Supprimer proprement les règles d'accès du contrôleur virtuel (`no wlan access-rule <role_name>`) lors de la suppression d'un profil.

---

## 5. Commandes de Diagnostic & Vérification sur l'AP

### Vérifier les clés MPSK actives
```text
show running-config | include mpsk
```
*Exemple de résultat attendu :*
```text
wlan mpsk-local MPSK_GUEST
 mpsk-local-passphrase gst_ac623199 a1b2c3d4e5f6... Guest-Standard
```

### Vérifier les contrats matériels de bande passante (Datapath BWM)
```text
show datapath bwm
```
*Exemple de résultat attendu :*
```text
ACL  DIR   Contract-ID  PerUser  UseCount  Rate(In Kbps)
---  ---   -----------  -------  --------  -------------
160  up    2            1        1         10000
160  down  1            1        1         10000
162  up    4            1        1         50000
162  down  3            1        1         50000
```

### Vérifier les clients connectés et leurs rôles
```text
show clients
```

---

## 6. Recommandations de Sécurité Complémentaires (Entreprise)

1. **Isolation stricte du réseau Invités (ACLs RFC1918)** :
   Pour interdire formellement aux invités d'accéder aux imprimantes, NAS et serveurs de l'entreprise même s'ils connaissent les adresses IP internes, vous pouvez enrichir les `wlan access-rule` :
   ```text
   wlan access-rule Guest-Standard
    rule any 10.0.0.0 255.0.0.0 match any any any deny
    rule any 172.16.0.0 255.240.0.0 match any any any deny
    rule any 192.168.0.0 255.255.0.0 match any any any deny
    rule any any match any any any permit
   ```

2. **Isolation Client à Client (Air Barrier)** :
   La directive `broadcast-filter all` présente dans le profil SSID empêche les diffusions inter-clients (ARP scan, mDNS, etc.) et isole complètement chaque appareil connecté sur la borne.
