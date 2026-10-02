# -*- coding: utf-8 -*-
"""Temoin hors machine : le SILENCE du portable declenche l'alerte.

Tourne sur un runner GitHub Actions planifie (workflow temoin.yml). A chaque passage :
  1. lit le battement pousse par le portable (branche `battement`, fichier battement.json) ;
  2. constate son age et ecrit la preuve datee, signee par le nom du runner qui observe
     (branche `preuve` : preuve.json + envoi.json, UN commit sans parent, reecrit a chaque
     passage : l'historique ne grossit pas) ;
  3. si le battement se tait depuis plus de 20 min, alerte sur Telegram, au plus une fois par
     heure. L'etat d'envoi est publie AVANT la tentative : sans publication, pas d'envoi.
Python 3, bibliotheque standard seule. `hote` vient toujours de platform.node()."""
import argparse, datetime, io, json, math, os, platform, subprocess, sys, tempfile, time
import urllib.parse, urllib.request

SILENCE_S = 1200        # 20 min sans battement : alerte
BORNE_ENVOI_S = 3600    # au plus une alerte par heure
CATALOGUE = ({"nom": "poste", "chemin": "battement.json", "lecture": "battement",
              "silence_s": SILENCE_S},)
LECTURES = ("battement",)


def _nombre_fini(valeur):
    if isinstance(valeur, bool):
        return None
    try:
        nombre = float(valeur)
    except (TypeError, ValueError, OverflowError):
        return None
    return nombre if math.isfinite(nombre) else None


def controle(chemin, t, silence):
    """Rend (alerte, detail). Battement frais : rien ; perime : POSTE_MUET ; absent :
    AUCUN_BATTEMENT ; illisible ou incoherent : BATTEMENT_ILLISIBLE."""
    if not os.path.exists(chemin):
        return True, {"alerte": "AUCUN_BATTEMENT", "motif": "aucun battement recu"}
    try:
        rec = json.load(io.open(chemin, encoding="utf-8"))
        if not isinstance(rec, dict):
            raise ValueError("la racine JSON n'est pas un objet")
        ts = _nombre_fini(rec.get("ts"))
        if ts is None:
            raise ValueError("champ ts absent, non numerique ou non fini")
        age = t - ts
        if age < 0:
            raise ValueError("champ ts futur : age negatif")
    except (OSError, ValueError, TypeError) as e:
        return True, {"alerte": "BATTEMENT_ILLISIBLE", "motif": "battement illisible : %s" % e}
    if age >= silence:
        return True, {"alerte": "POSTE_MUET", "age_s": round(age, 1), "silence_s": silence,
                      "dernier_ts": ts, "motif": "pas de battement depuis %.1f min" % (age / 60.0)}
    return False, {"alerte": None, "age_s": round(age, 1), "silence_s": silence,
                   "motif": "battement frais"}


def lit_catalogue(chemin):
    """Liste JSON non vide d'entrees {nom, chemin, lecture, silence_s}. silence_s doit etre un
    NOMBRE (un texte comme "1200" est refuse) : toute entree invalide refuse tout (ValueError)."""
    data = json.load(io.open(chemin, encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("catalogue : liste non vide attendue")
    vus = set()
    for e in data:
        s = e.get("silence_s") if isinstance(e, dict) else None
        ok = isinstance(e, dict) and isinstance(s, (int, float)) and not isinstance(s, bool) \
            and _nombre_fini(s) is not None and s > 0 \
            and isinstance(e.get("nom"), str) and e["nom"] and e["nom"] not in vus \
            and isinstance(e.get("chemin"), str) and e["chemin"] and e.get("lecture") in LECTURES
        if not ok:
            raise ValueError("catalogue : entree invalide ou nom en double : %r" % (e,))
        vus.add(e["nom"])
    return tuple(data)


def observe(racine, t, catalogue=CATALOGUE, hote=None):
    sources, alerte = {}, False
    for e in catalogue:
        a, det = controle(os.path.join(racine, e["chemin"]), t, float(e["silence_s"]))
        sources[e["nom"]] = det
        alerte = alerte or a
    ts = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return {"hote": hote or platform.node(), "ts": ts.isoformat(), "ts_epoch": t,
            "alerte": bool(alerte), "sources": sources, "observateur": "temoin.py"}


def decide_envoi(rec, avant, t):
    """(envoyer, etat d'envoi a publier). Une alerte par heure au plus."""
    if rec["alerte"] and t - avant >= BORNE_ENVOI_S:
        return True, {"dernier_envoi_ts": t, "envoi": "TENTATIVE"}
    return False, {"dernier_envoi_ts": avant,
                   "envoi": "BORNE" if rec["alerte"] else None}


def texte_alerte(rec):
    lignes = []
    for nom, d in sorted(rec["sources"].items()):
        if d.get("alerte") == "POSTE_MUET":
            heure = datetime.datetime.fromtimestamp(d["dernier_ts"], datetime.timezone.utc)
            lignes.append("le portable ne donne plus signe de vie depuis %d min (dernier signe à %s UTC)."
                          % (d["age_s"] // 60, heure.strftime("%d/%m %H:%M")))
        elif d.get("alerte"):
            lignes.append("aucun signe de vie lisible du portable (%s)." % d["alerte"])
    return ("Témoin hors machine : " + " ".join(lignes) +
            "\nIl est peut-être éteint, figé ou sans réseau. Prochaine alerte dans une heure au plus tôt.")


def git(*args, entree=None, env=None, octets=False):
    if octets:
        return subprocess.run(["git"] + list(args), capture_output=True, timeout=120, env=env)
    return subprocess.run(["git"] + list(args), input=entree, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, env=env)


def lit_branche(branche, fichier):
    """(statut, octets). OK : octets bruts (jamais decodes ici : un battement en UTF-16 doit finir
    ILLISIBLE, pas absent) ; ABSENT : la branche n'existe pas ; ECHEC : reseau ou lecture ratee
    (ne JAMAIS confondre avec ABSENT : on remettrait la borne d'envoi a zero)."""
    rc = git("ls-remote", "--exit-code", "origin", "refs/heads/" + branche).returncode
    if rc == 2:
        return "ABSENT", None
    if rc != 0 or git("fetch", "--quiet", "--depth=1", "origin", branche).returncode != 0:
        return "ECHEC", None
    r = git("show", "FETCH_HEAD:" + fichier, octets=True)
    return ("OK", r.stdout) if r.returncode == 0 else ("ECHEC", None)


def publie(fichiers, t):
    """Un commit SANS parent portant `fichiers` ({nom: objet}), pousse en force sur `preuve`."""
    d = tempfile.mkdtemp()
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(d, "index"),
               GIT_AUTHOR_NAME="temoin", GIT_AUTHOR_EMAIL="temoin@localhost",
               GIT_COMMITTER_NAME="temoin", GIT_COMMITTER_EMAIL="temoin@localhost")
    for nom, obj in sorted(fichiers.items()):
        r = git("hash-object", "-w", "--stdin",
                entree=json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", env=env)
        if r.returncode != 0 or git("update-index", "--add", "--cacheinfo",
                                    "100644,%s,%s" % (r.stdout.strip(), nom), env=env).returncode != 0:
            return False
    arbre = git("write-tree", env=env)
    if arbre.returncode != 0:
        return False
    c = git("commit-tree", arbre.stdout.strip(), "-m", "preuve %d" % t, env=env)
    if c.returncode != 0:
        return False
    return git("push", "--quiet", "--force", "origin",
               "%s:refs/heads/preuve" % c.stdout.strip()).returncode == 0


def envoie(texte):
    fictif = os.environ.get("TEMOIN_TG_FICTIF")
    if fictif:
        with io.open(fictif, "a", encoding="utf-8") as f:
            f.write(texte + "\n---\n")
        return "FICTIF"
    jeton, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not jeton or not chat:
        return "ECHEC secrets absents"
    corps = urllib.parse.urlencode({"chat_id": chat, "text": texte}).encode("utf-8")
    try:
        with urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % jeton,
                                    corps, timeout=30) as r:
            return "ENVOYE" if json.load(r).get("ok") else "ECHEC reponse"
    except Exception as e:  # jamais le message : il pourrait citer l'adresse avec le jeton
        return "ECHEC %s" % type(e).__name__


def main(argv=None):
    ap = argparse.ArgumentParser(description="Temoin hors machine (runner GitHub).")
    ap.add_argument("--catalogue", default="")
    ap.add_argument("--maintenant", type=float, default=None)
    a = ap.parse_args(argv)
    t = _nombre_fini(a.maintenant if a.maintenant is not None else time.time())
    if t is None:
        print("OBSERVATION_REFUS horloge invalide")
        return 2
    try:
        cat = lit_catalogue(a.catalogue) if a.catalogue else CATALOGUE
    except (OSError, ValueError, TypeError) as e:
        print("OBSERVATION_REFUS catalogue illisible : %s" % e)
        return 2
    miroir = tempfile.mkdtemp()
    statut, battement = lit_branche("battement", "battement.json")
    statut_e, envoi_brut = lit_branche("preuve", "envoi.json")
    if "ECHEC" in (statut, statut_e):
        print("LECTURE_ECHEC battement=%s preuve=%s : rien publie, aucun envoi" % (statut, statut_e))
        return 3
    if battement is not None:
        with io.open(os.path.join(miroir, "battement.json"), "wb") as f:
            f.write(battement)
    rec = observe(miroir, t, cat)
    try:
        avant = _nombre_fini(json.loads((envoi_brut or b"{}").decode("utf-8"))
                             .get("dernier_envoi_ts")) or 0.0
    except (ValueError, AttributeError):  # UnicodeDecodeError est une ValueError
        avant = 0.0
    envoyer, etat = decide_envoi(rec, avant, t)
    if not publie({"preuve.json": rec, "envoi.json": etat}, t):
        print("PUBLICATION_ECHEC : preuve non poussee, aucun envoi")
        return 3
    resultat = envoie(texte_alerte(rec)) if envoyer else etat["envoi"] or "-"
    muets = sorted(n for n, d in rec["sources"].items() if d.get("alerte"))
    print("OBSERVATION hote=%s ts=%s alerte=%s sources_en_alerte=%s envoi=%s"
          % (rec["hote"], rec["ts"], rec["alerte"], ",".join(muets) or "-", resultat))
    # envoi rate : le job echoue, GitHub previent le proprietaire par courriel (escalade)
    return 4 if str(resultat).startswith("ECHEC") else 0


if __name__ == "__main__":
    sys.exit(main())
