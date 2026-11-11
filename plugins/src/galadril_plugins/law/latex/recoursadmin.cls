\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{recoursadmin}[2026/11/05 v2.7 Administrative litigation]

\LoadClass[a4paper,fontsize=11pt,DIV=12,BCOR=0mm,parskip=half,
  headings=normal,numbers=noenddot]{scrartcl}
\RequirePackage{iftex}
\ifPDFTeX
  \RequirePackage[T1]{fontenc}
  \RequirePackage[utf8]{inputenc}
\fi
\RequirePackage[french]{babel}
\IfFileExists{fourier.sty}{\RequirePackage{fourier}}{\RequirePackage{mathpazo}}
\ifXeTeX
  \RequirePackage[expansion=false,final]{microtype}
\else
  \RequirePackage[activate={true,nocompatibility},final]{microtype}
\fi
\RequirePackage{csquotes}
\RequirePackage{enumitem}
\RequirePackage{array}
\RequirePackage{amsmath}
\RequirePackage{xcolor}
\RequirePackage{needspace}
\RequirePackage{lastpage}
\RequirePackage{scrlayer-scrpage}
\RequirePackage{etoolbox}
\RequirePackage{xparse}
\RequirePackage[unicode=true,pdfencoding=auto,hidelinks]{hyperref}
\RequirePackage{bookmark}
\RequirePackage{graphicx}

\newcommand{\fontelogo}{\sffamily\bfseries}

\setquotestyle{french}
\definecolor{TitreTexte}{HTML}{20242A}
\definecolor{MetaTexte}{HTML}{626A73}
\definecolor{FondTitre}{HTML}{F5F6F7}
\definecolor{Filet}{HTML}{B9BEC5}
\definecolor{LienJurisprudence}{HTML}{264866}

\setlength{\parindent}{0pt}
\linespread{1.035}\selectfont
\emergencystretch=2.5em
\widowpenalty=10000
\clubpenalty=10000
\displaywidowpenalty=10000
\setkomafont{disposition}{\normalfont\bfseries\color{TitreTexte}}
\setkomafont{section}{\normalfont\bfseries\large\color{TitreTexte}}
\setkomafont{subsection}{\normalfont\bfseries\normalsize\color{TitreTexte}}
\setkomafont{subsubsection}{\normalfont\bfseries\normalsize\color{TitreTexte}}
\setkomafont{paragraph}{\normalfont\bfseries\normalsize\color{TitreTexte}}

\setcounter{secnumdepth}{4}
\setcounter{tocdepth}{3}
\renewcommand*{\thesection}{\Roman{section}}
\renewcommand*{\thesubsection}{\Alph{subsection}}
\renewcommand*{\thesubsubsection}{\arabic{subsubsection}}
\renewcommand*{\theparagraph}{\alph{paragraph}}
\renewcommand*{\theHsection}{\arabic{section}}
\renewcommand*{\theHsubsection}{\arabic{section}.\arabic{subsection}}
\renewcommand*{\theHsubsubsection}{\arabic{section}.\arabic{subsection}.\arabic{subsubsection}}
\renewcommand*{\theHparagraph}{\arabic{section}.\arabic{subsection}.\arabic{subsubsection}.\arabic{paragraph}}
\renewcommand*{\sectionformat}{\thesection.\enskip}
\renewcommand*{\subsectionformat}{\thesubsection.\enskip}
\renewcommand*{\subsubsectionformat}{\thesubsubsection.\enskip}
\renewcommand*{\paragraphformat}{\theparagraph)\enskip}
\RedeclareSectionCommand[beforeskip=1.45\baselineskip,afterskip=.5\baselineskip]{section}
\RedeclareSectionCommand[beforeskip=.86\baselineskip,afterskip=.20\baselineskip]{subsection}
\RedeclareSectionCommand[beforeskip=.68\baselineskip,afterskip=.15\baselineskip]{subsubsection}
\RedeclareSectionCommand[beforeskip=.52\baselineskip,afterskip=1em]{paragraph}

\makeatletter
\renewcommand*{\sectionlinesformat}[4]{%
  \@hangfrom{\hskip #2#3}{%
    \Ifstr{#1}{section}{\MakeUppercase{#4}}{#4}}%
  \Ifstr{#1}{section}{%
    \par\nobreak\vspace{.29\baselineskip}%
    {\color{Filet}\hrule height .45pt}}{}%
}
\makeatother
\setlist[itemize]{leftmargin=1.55em,itemsep=.28em,topsep=.36em,
  parsep=0pt,partopsep=0pt,label=\textendash}
\setlist[enumerate]{itemsep=.34em,topsep=.38em,parsep=0pt,partopsep=0pt}

\makeatletter
\newcommand{\ra@nom}{}
\newcommand{\ra@civilite}{}
\newcommand{\ra@adresse}{}
\newcommand{\ra@email}{}
\newcommand{\ra@qualite}{}
\newcommand{\ra@defendeur}{}
\newcommand{\ra@definitiondefendeur}{}
\newcommand{\ra@siege}{}
\newcommand{\ra@representation}{}
\newcommand{\ra@juridiction}{}
\newcommand{\ra@juridictioncourte}{}
\newcommand{\ra@adressegreffe}{}
\newcommand{\ra@ville}{}
\newcommand{\ra@date}{\today}
\newcommand{\ra@numero}{}
\newcommand{\ra@mode}{fond}
\newcommand{\ra@nature}{requete}
\newcommand{\ra@typememoire}{complementaire}
\newcommand{\ra@nummemoire}{}
\newcommand{\ra@soustitre}{}
\newcommand{\ra@titrelibre}{}
\newcommand{\ra@objet}{}
\newcommand{\ra@recipient}{}
\newcommand{\ra@qualifentete}{}

\newcommand{\ra@logo}{}
\newcommand{\ra@textelogo}{}
\newcommand{\ra@direction}{}
\newcommand{\ra@suivipar}{}

\newcommand{\DefinirEnteteInstitutionnel}[4]{%
  \gdef\ra@logo{#1}%
  \gdef\ra@textelogo{#2}%
  \gdef\ra@direction{#3}%
  \gdef\ra@suivipar{#4}%
}

\newcommand{\DefinirRequerant}[5]{%
  \gdef\ra@nom{#1}\gdef\ra@civilite{#2}\gdef\ra@adresse{#3}%
  \gdef\ra@email{#4}\gdef\ra@qualite{#5}}
\newcommand{\DefinirDefendeur}[4]{%
  \gdef\ra@defendeur{#1}\gdef\ra@definitiondefendeur{#2}%
  \gdef\ra@siege{#3}\gdef\ra@representation{#4}}
\newcommand{\DefinirJuridiction}[3]{%
  \gdef\ra@juridiction{#1}\gdef\ra@juridictioncourte{#2}%
  \gdef\ra@adressegreffe{#3}}

\newcommand{\DeclarerDossier}[3]{%
  \expandafter\gdef\csname ra@num@#1\endcsname{#2}%
  \expandafter\gdef\csname ra@mode@#1\endcsname{#3}}
\newcommand{\ChoisirDossier}[1]{%
  \ifcsdef{ra@mode@#1}{%
    \expandafter\let\expandafter\ra@numero\csname ra@num@#1\endcsname
    \expandafter\let\expandafter\ra@mode\csname ra@mode@#1\endcsname
  }{\ClassError{recoursadmin}{Dossier `#1' inconnu}{Le declarer dans information.tex.}}}
\newcommand{\NatureActe}[1]{\gdef\ra@nature{#1}}
\newcommand{\ModeRefere}[1]{\gdef\ra@mode{#1}}
\newcommand{\ModeFond}{\gdef\ra@mode{fond}}
\newcommand{\ModeAutre}{\gdef\ra@mode{autre}}
\newcommand{\TypeMemoire}[1]{\gdef\ra@typememoire{#1}}
\newcommand{\NumeroMemoire}[1]{\gdef\ra@nummemoire{#1}}
\newcommand{\SousTitreDocument}[1]{\gdef\ra@soustitre{#1}}
\newcommand{\TitreDocumentLibre}[1]{\gdef\ra@titrelibre{#1}}
\newcommand{\ObjetActe}[1]{\gdef\ra@objet{#1}}
\newcommand{\DateDepot}[1]{\gdef\ra@date{#1}}
\newcommand{\VilleSignature}[1]{\gdef\ra@ville{#1}}
\newcommand{\DestinataireCourrier}[1]{\gdef\ra@recipient{#1}}
\newcommand{\QualificationEntete}[1]{\gdef\ra@qualifentete{#1}}

\newcommand{\Requerant}{\ra@nom}
\newcommand{\Defendeur}{\ra@defendeur}
\newcommand{\NumeroDossier}{\ra@numero}
\newcommand{\Juridiction}{\ra@juridiction}
\newcommand{\DateDocument}{\ra@date}
\newcommand{\NumeroDossierAffiche}{%
  \ifdefstring{\ra@mode}{autre}{}{%
    \ifdefempty{\ra@numero}{instance non encore enregistrée}{\no~\ra@numero}%
  }%
}

\newcommand{\ra@nomregime}{%
 \ifdefstring{\ra@mode}{autre}{}{%
 \ifdefstring{\ra@mode}{fond}{Recours pour excès de pouvoir}{%
 \ifdefstring{\ra@mode}{suspension}{Référé-suspension}{%
 \ifdefstring{\ra@mode}{liberte}{Référé-liberté}{%
 \ifdefstring{\ra@mode}{conservatoire}{Référé conservatoire (mesures utiles)}{%
 \ifdefstring{\ra@mode}{mesures-utiles}{Référé mesures utiles}{%
 \ifdefstring{\ra@mode}{modification}{Référé relatif à la modification de mesures}{%
 \ClassError{recoursadmin}{Regime inconnu `\ra@mode'}{Verifier information.tex.}}}}}}}}}

\newcommand{\ra@fondement}{%
 \ifdefstring{\ra@mode}{autre}{}{%
 \ifdefstring{\ra@mode}{fond}{Recours pour excès de pouvoir}{%
 \ifdefstring{\ra@mode}{suspension}{Référé-suspension -- article L.~521-1 du code de justice administrative}{%
 \ifdefstring{\ra@mode}{liberte}{Référé-liberté -- article L.~521-2 du code de justice administrative}{%
 \ifdefstring{\ra@mode}{conservatoire}{Référé conservatoire -- article L.~521-3 du code de justice administrative}{%
 \ifdefstring{\ra@mode}{mesures-utiles}{Référé mesures utiles -- article L.~521-3 du code de justice administrative}{%
 \ifdefstring{\ra@mode}{modification}{Modification de mesures -- article L.~521-4 du code de justice administrative}{}}}}}}}}

\newcommand{\ra@nommemoire}{%
 \ifdefstring{\ra@typememoire}{replique}{\MakeUppercase{Mémoire en réplique}}{%
 \ifdefstring{\ra@typememoire}{defense}{\MakeUppercase{Mémoire en défense}}{%
 \ifdefstring{\ra@typememoire}{recapitulatif}{\MakeUppercase{Mémoire récapitulatif}}{%
 \ifdefstring{\ra@typememoire}{observations}{\MakeUppercase{Mémoire en observations}}{\MakeUppercase{Mémoire complémentaire}}}}}%
 \ifdefempty{\ra@nummemoire}{}{~\No~\ra@nummemoire}}

\newcommand{\ra@titreautomatique}{%
 \ifdefstring{\ra@nature}{courrier}{%
   \ifdefstring{\ra@mode}{autre}{\MakeUppercase{Correspondance}}{\MakeUppercase{Correspondance contentieuse}}%
 }{%
 \ifdefstring{\ra@nature}{requete}{%
    \ifdefstring{\ra@mode}{fond}{\MakeUppercase{Requête en annulation pour excès de pouvoir}}{%
      \ifdefstring{\ra@mode}{suspension}{\MakeUppercase{Requête en référé-suspension}}{%
        \ifdefstring{\ra@mode}{liberte}{\MakeUppercase{Requête en référé-liberté}}{%
          \ifdefstring{\ra@mode}{conservatoire}{\MakeUppercase{Requête en référé conservatoire}}{%
          \ifdefstring{\ra@mode}{mesures-utiles}{\MakeUppercase{Requête en référé mesures utiles}}{%
            \ifdefstring{\ra@mode}{modification}{\MakeUppercase{Requête en modification de mesures}}{\MakeUppercase{Document libre}}}}}}}}{%
 \ifdefstring{\ra@nature}{memoire}{\ra@nommemoire}{%
 \ifdefstring{\ra@nature}{note}{\MakeUppercase{Note}}{%
 \MakeUppercase{Document}}}}}}

\newcommand{\ra@titre}{%
 \ifdefempty{\ra@titrelibre}{\ra@titreautomatique}{\MakeUppercase{\ra@titrelibre}}}

\newcommand{\ra@surtitre}{%
  \ifdefstring{\ra@nature}{requete}{%
    \MakeUppercase{Devant le \ra@juridiction}}{%
  \ifdefstring{\ra@nature}{memoire}{%
    \ifdefstring{\ra@mode}{fond}{\MakeUppercase{Au recours pour excès de pouvoir}}{%
    \ifdefstring{\ra@mode}{suspension}{\MakeUppercase{En référé-suspension}}{%
    \ifdefstring{\ra@mode}{liberte}{\MakeUppercase{En référé-liberté}}{%
    \ifdefstring{\ra@mode}{conservatoire}{\MakeUppercase{En référé conservatoire}}{%
    \ifdefstring{\ra@mode}{mesures-utiles}{\MakeUppercase{En référé mesures utiles}}{%
    \ifdefstring{\ra@mode}{modification}{\MakeUppercase{En référé de modification de mesures}}{}}}}}}}{}}}

\newcommand{\ra@titrebas}{%
  \ifdefstring{\ra@nature}{memoire}{%
    \ifdefstring{\ra@typememoire}{replique}{Mémoire en réplique}{%
    \ifdefstring{\ra@typememoire}{defense}{Mémoire en défense}{%
    \ifdefstring{\ra@typememoire}{recapitulatif}{Mémoire récapitulatif}{%
    \ifdefstring{\ra@typememoire}{observations}{Mémoire en observations}{Mémoire complémentaire}}}}%
    \ifdefempty{\ra@nummemoire}{}{~\no~\ra@nummemoire}}{%
  \ifdefstring{\ra@nature}{requete}{\ra@nomregime}{%
  \ifdefstring{\ra@nature}{note}{Note}{Correspondance}}}}

\newcommand{\ra@piedauto}{%
  \ifdefstring{\ra@mode}{autre}{%
    \ifdefempty{\ra@titrelibre}{Document}{\ra@titrelibre} -- \ra@nom%
  }{%
    \ifdefstring{\ra@nature}{requete}{%
      \ra@titrebas{} -- \ra@nom{} c/ \ra@defendeur}{%
      \ra@titrebas{} -- \ra@juridictioncourte{} \NumeroDossierAffiche}%
  }%
}

\AtBeginDocument{%
 \hypersetup{pdftitle={\ifdefempty{\ra@titrelibre}{Document}{\ra@titrelibre} -- \ra@nom},
    pdfauthor={\ra@nom},pdfsubject={\ra@juridiction}}%
 \ifoot*{\ra@piedauto}%
}
\makeatother

\clearpairofpagestyles
\KOMAoptions{footsepline=.25pt}
\setkomafont{pageheadfoot}{\normalfont\footnotesize\color{MetaTexte}}
\setkomafont{pagefoot}{\normalfont\footnotesize\color{MetaTexte}}
\setkomafont{footsepline}{\color{Filet}}
\ofoot*{\pagemark/\pageref*{LastPage}}
\pagestyle{scrheadings}

\makeatletter
\newcommand{\ra@cartouchetitre}{%
 \begin{center}%
 \begingroup\setlength{\fboxsep}{8pt}%
 \fcolorbox{Filet}{FondTitre}{\parbox{.84\textwidth}{%
   \centering\color{TitreTexte}%
   {\bfseries\large \ra@titre\par}%
   \ifdefempty{\ra@titrelibre}{%
     \ifdefstring{\ra@nature}{requete}{%
       \vspace{.18em}{\bfseries\normalsize\ra@surtitre\par}}{%
     \ifdefstring{\ra@nature}{memoire}{%
       \vspace{.15em}{\bfseries\normalsize\ra@surtitre\par}}{}}}{}%
   \ifdefempty{\ra@soustitre}{}{%
     \vspace{.25em}{\bfseries\normalsize\ra@soustitre\par}}%
 }}%
 \endgroup\end{center}}

\newcommand{\ra@pourcontre}{%
 \vspace{.75em}%
 {\bfseries\color{TitreTexte}POUR :}\par
 \ifdefstring{\ra@nature}{requete}{%
   \textbf{\ra@civilite{} \ra@nom}, demeurant \ra@adresse,
   courriel : \ra@email, \ra@qualite.\par
 }{\textbf{\ra@civilite{} \ra@nom}, requérant, \ra@qualite.\par}%
 \vspace{.55em}%
 {\bfseries\color{TitreTexte}CONTRE :}\par
 \textbf{\ra@defendeur}%
 \ifdefstring{\ra@nature}{requete}{%
   \ifdefempty{\ra@definitiondefendeur}{}{, \ra@definitiondefendeur}%
   \ifdefempty{\ra@siege}{}{, dont le siège est situé \ra@siege}%
 }{}%
 \ifdefempty{\ra@representation}{}{, \ra@representation}.\par}

\newcommand{\EnteteContentieux}{%
 {\small\color{MetaTexte}%
   \textbf{\color{TitreTexte}\ra@nom{} / \MakeUppercase{\ra@defendeur}}\\[-.05em]
   \ifdefempty{\ra@qualifentete}{\ra@fondement}{\ra@qualifentete}\\[-.05em]
   \ra@juridiction%
   \ifdefempty{\ra@numero}{%
     \ifdefstring{\ra@mode}{autre}{}{ -- instance non encore enregistrée}%
   }{ -- dossier \no~\ra@numero}\par}%
 \vspace{.90cm}\ra@cartouchetitre\vspace{.75cm}%
 \ra@pourcontre
 \ifdefempty{\ra@objet}{}{\vspace{.75em}\textbf{OBJET :}\par \ra@objet\par}%
 \vspace{.75em}}

\newcommand{\ra@afficherenteteinst}{%
  \begin{minipage}[t]{.58\textwidth}
    \raggedright
    \ifdefempty{\ra@logo}{}{%
      \raisebox{-.35\height}{\includegraphics[height=3cm]{\ra@logo}}\enskip
    }%
    \ifdefempty{\ra@textelogo}{}{%
      \begin{minipage}[c]{.65\textwidth}
        {\fontelogo\large\color{TitreTexte}\ra@textelogo}%
      \end{minipage}%
    }%
    \par\vspace{-1.5em}%
    \ifdefempty{\ra@direction}{}{%
      {\bfseries\color{TitreTexte}\ra@direction}\par\vspace{.6em}%
    }%
    {\itshape\bfseries Dossier suivi par :} \\
    \ifdefempty{\ra@suivipar}{\ra@nom \\ \ra@email}{\ra@suivipar}%
  \end{minipage}%
}

\newcommand{\EnteteCourrier}{%
 {\small\color{MetaTexte}%
   \ifdefempty{\ra@logo}{%
     \ifdefempty{\ra@textelogo}{%
       \textbf{\color{TitreTexte}\ra@nom}\\[-.05em]
       \ra@adresse\\[-.05em]
       \ra@email\par
     }{%
       \ra@afficherenteteinst
     }%
   }{%
     \ra@afficherenteteinst
   }%
 }%
 \vspace{.78cm}%
 \begin{flushright}\begin{minipage}{.49\textwidth}\raggedright
   \ifdefempty{\ra@recipient}{\ra@adressegreffe}{\ra@recipient}%
 \end{minipage}\end{flushright}
 \vspace{.20cm}\hfill \ifdefempty{\ra@ville}{}{\ra@ville, }le \ra@date\par
 \vspace{.66cm}\ra@cartouchetitre\vspace{.34cm}%
 \ifdefstring{\ra@mode}{autre}{}{%
   \textbf{\ifdefstring{\ra@mode}{fond}{Recours en annulation}{\ra@nomregime} \NumeroDossierAffiche}%
     {} -- \ra@nom{} c/ \ra@defendeur\par
 }%
 \ifdefempty{\ra@objet}{}{\textbf{Objet :} \ra@objet\par}%
 \vspace{.75em}}

\newcommand{\EnteteDocument}{%
 \ifdefstring{\ra@nature}{courrier}{\EnteteCourrier}{\EnteteContentieux}}

\newcommand{\SalutationCourrier}[1][Madame, Monsieur,]{#1\par\medskip}
\newcommand{\FormuleCourrier}[1][Je vous prie d'agréer, Madame, Monsieur, l'expression de ma considération respectueuse.]{%
 \par\medskip #1\par\SignatureCourrier}
\makeatother

\newcommand{\TitrePartie}[1]{\needspace{5\baselineskip}\section{#1}}
\newcommand{\SousTitre}[1]{\needspace{3\baselineskip}\subsection{#1}}
\newcommand{\SousSousTitre}[1]{\needspace{3\baselineskip}\subsubsection{#1}}

\newcounter{paragraphecontentieux}
\newif\ifParagraphesNumerotes
\ParagraphesNumerotesfalse
\newcommand{\NumeroterParagraphes}{\ParagraphesNumerotestrue}
\newcommand{\NePasNumeroterParagraphes}{\ParagraphesNumerotesfalse}
\newcommand{\ParagrapheNumerote}{%
  \par\addvspace{.12\baselineskip}%
  \ifParagraphesNumerotes
    \refstepcounter{paragraphecontentieux}%
    \noindent\textbf{\arabic{paragraphecontentieux}.}\enspace
  \else\noindent\fi}
\newcommand{\ReinitialiserParagraphes}{\setcounter{paragraphecontentieux}{0}}

\RenewDocumentEnvironment{quote}{o +b}{%
  \par\addvspace{.65\baselineskip}%
  \begin{addmargin}[1.15em]{1.15em}%
  \noindent\begingroup\small\itshape\color{TitreTexte}%
  \enquote{#2}\par\endgroup
  \IfNoValueF{#1}{%
    \par\smallskip\noindent\hfill
    \begin{minipage}{.94\linewidth}\raggedleft\footnotesize\normalfont
      \color{MetaTexte}#1\end{minipage}\par}%
  \end{addmargin}%
  \par\addvspace{.38\baselineskip}%
}{}
\NewDocumentEnvironment{citationjuridique}{o +b}{%
  \IfNoValueTF{#1}{\begin{quote}#2\end{quote}}%
    {\begin{quote}[#1]#2\end{quote}}%
}{}
\newcommand{\ParCesMotifs}{%
 \par\addvspace{1.45em}\begin{center}
 {\large\bfseries\color{TitreTexte}\MakeUppercase{Par ces motifs :}}
 \end{center}\vspace{-.15em}{\color{Filet}\hrule height .55pt}
 \vspace{.55em}}
\makeatletter
% Opt-in widgets preserve ordinary exports and avoid an unavailable eforms bundle.
\newif\ifGaladrilSignatureFields
\newcommand{\GaladrilEnableSignatureFields}{%
  \ifXeTeX
    \ifGaladrilSignatureFields\else
      \GaladrilSignatureFieldstrue
      \AddToHook{shipout/firstpage}{%
        \special{pdf:obj @galadrilsigfields []}%
        \special{pdf:put @catalog << /AcroForm << /Fields @galadrilsigfields /SigFlags 3 >> >>}}%
    \fi
  \else
    \ClassError{recoursadmin}{Signature fields require XeTeX}{Compile with Tectonic.}%
  \fi}
\@onlypreamble\GaladrilEnableSignatureFields
\ExplSyntaxOn
\prop_new:N \g_ra_signature_slots_prop
\NewDocumentCommand{\GaladrilSignatureSlot}{mmm}
 {
  \ifGaladrilSignatureFields
    \regex_match:nnTF {\A[a-z][a-z0-9\-]{0,55}\Z} {#1}
      {
        \prop_if_in:NnTF \g_ra_signature_slots_prop {#1}
          {\ClassError{recoursadmin}{Duplicate~signature~field}{Use~unique~names.}}
          {
            \prop_gput:Nnn \g_ra_signature_slots_prop {#1} {}
            \leavevmode\hbox_to_wd:nn {#2}
              {
                \vrule width 0pt height #3 depth 0pt
                \special{pdf:ann~@galadrilsig#1~width~\dim_eval:n{#2}~
                  height~\dim_eval:n{#3}~depth~0pt~
                  <</Subtype~/Widget~/FT~/Sig~/T~(galadril:#1)~/F~4~/P~@thispage>>}
                \special{pdf:put~@galadrilsigfields~@galadrilsig#1}
                \hfil
              }
          }
      }
      {\ClassError{recoursadmin}{Invalid~signature~field~name}{Use~lowercase~ASCII,~digits~and~hyphens.}}
  \fi
 }
\ExplSyntaxOff
\newcommand{\SignatureRequerant}{%
 \par\addvspace{1.45em}\ifdefempty{\ra@ville}{Le}{Fait à \ra@ville, le} \ra@date.\par
 \ifGaladrilSignatureFields\else\vspace{1.4cm}\fi\hfill\begin{minipage}{.42\textwidth}
 \centering
 \ifGaladrilSignatureFields\GaladrilSignatureSlot{claimant}{136bp}{34bp}\par\fi
 \ra@nom\end{minipage}\par}
\newcommand{\SignatureCourrier}{%
 \par\addvspace{1.30em}\hfill
 \begin{minipage}{.42\textwidth}\centering
 \ifGaladrilSignatureFields\GaladrilSignatureSlot{sender}{136bp}{34bp}\par\fi
 \ra@nom\end{minipage}\par}
\makeatother
\newcommand{\TitreBordereau}[1]{%
 \begin{center}{\large\bfseries\color{TitreTexte}\MakeUppercase{#1}}\end{center}
 \vspace{-.15em}{\color{Filet}\hrule height .55pt}\vspace{.65em}}

\ExplSyntaxOn
\seq_new:N \g_ra_pieces_seq
\prop_new:N \g_ra_pieces_prop
\int_new:N \g_ra_piece_compteur_int
\cs_new_protected:Npn \ra_declarer_piece:nn #1#2
 {
  \prop_if_in:NnTF \g_ra_pieces_prop {#1}
   {\ClassError{recoursadmin}{Piece~#1~deja~declaree}{Ne~la~declarer~qu'une~fois~par~document.}}
   {
    \prop_gput:Nnn \g_ra_pieces_prop {#1} {#2}
    \seq_gput_right:Nn \g_ra_pieces_seq {#1}
    \int_compare:nNnT {#1} > {\g_ra_piece_compteur_int}
      {\int_gset:Nn \g_ra_piece_compteur_int {#1}}
   }
 }
\NewDocumentCommand{\DeclarerPiece}{mm}{\ra_declarer_piece:nn {#1}{#2}}
\NewDocumentCommand{\AjouterPiece}{m}
 {
   \int_gincr:N \g_ra_piece_compteur_int
   \exp_args:Nx \ra_declarer_piece:nn {\int_use:N \g_ra_piece_compteur_int} {#1}
 }
\NewExpandableDocumentCommand{\DernierePiece}{}{\int_use:N \g_ra_piece_compteur_int}
\NewDocumentCommand{\RefPiece}{m}{pièce~\no~#1}

\cs_new_protected:Npn \ra_citation_piece:n #1
 {
  \par\addvspace{.38\baselineskip}
  \begingroup
    \raggedleft\footnotesize\itshape\color{MetaTexte}
    Pièce~\no~#1~:~%
    \prop_if_in:NnTF \g_ra_pieces_prop {#1}
      {\prop_item:Nn \g_ra_pieces_prop {#1}}
      {\ClassWarning{recoursadmin}{Piece~#1~non~declaree}%
       [intitulé~à~compléter]}
    \par
  \endgroup
  \addvspace{.34\baselineskip}
 }
\NewDocumentCommand{\CitationPiece}{m}{\exp_args:Nx \ra_citation_piece:n {#1}}
\NewCommandCopy{\pieceint}{\CitationPiece}

\NewDocumentCommand{\BordereauPieces}{}
 {
  \TitreBordereau{Bordereau~des~pièces~:}
  \seq_if_empty:NTF \g_ra_pieces_seq
   {Aucune~pièce~déclarée~dans~cette~écriture.}
   {
    \begin{description}[
      font=\bfseries,
      style=multiline,
      align=left,
      leftmargin=2.45cm,
      labelwidth=2.20cm,
      labelsep=.25cm,
      topsep=.20em,
      itemsep=.45em,
      parsep=0pt,
      partopsep=0pt]
      \seq_map_inline:Nn \g_ra_pieces_seq
       {\item[{\makebox[2.20cm][r]{Pièce~\no~##1~:}}] \prop_item:Nn \g_ra_pieces_prop {##1}}
    \end{description}
   }
 }

\prop_new:N \g_ra_arrets_prop
\bool_new:N \g_ra_arrets_liens_bool
\bool_gset_true:N \g_ra_arrets_liens_bool
\NewDocumentCommand{\DesactiverLiensJurisprudence}{}{\bool_gset_false:N \g_ra_arrets_liens_bool}
\NewDocumentCommand{\ActiverLiensJurisprudence}{}{\bool_gset_true:N \g_ra_arrets_liens_bool}
\tl_new:N \l_ra_arret_titre_tl
\tl_new:N \l_ra_arret_ecli_tl
\tl_new:N \l_ra_arret_url_tl
\tl_new:N \l_ra_arret_id_tl
\tl_new:N \l_ra_arret_fonds_tl
\tl_new:N \l_ra_arret_destination_tl
\keys_define:nn {ra/arret}
 {
   titre .tl_set:N = \l_ra_arret_titre_tl,
   ecli .tl_set:N = \l_ra_arret_ecli_tl,
   url .tl_set:N = \l_ra_arret_url_tl,
   legifrance .tl_set:N = \l_ra_arret_id_tl,
   fonds .choice:,
   fonds / ceta .code:n = {\tl_set:Nn \l_ra_arret_fonds_tl {ceta}},
   fonds / juri .code:n = {\tl_set:Nn \l_ra_arret_fonds_tl {juri}},
 }
\cs_new_protected:Npn \ra_arret_enregistrer:nnnnn #1#2#3#4#5
 {
  \prop_if_in:NnTF \g_ra_arrets_prop {#1/juridiction}
   {\ClassError{recoursadmin}{Arret~`#1'~deja~declare}{Chaque~cle~doit~etre~unique.}}
   {
    \tl_clear:N \l_ra_arret_titre_tl
    \tl_clear:N \l_ra_arret_ecli_tl
    \tl_clear:N \l_ra_arret_url_tl
    \tl_clear:N \l_ra_arret_id_tl
    \tl_set:Nn \l_ra_arret_fonds_tl {ceta}
    \keys_set:nn {ra/arret} {#5}
    \prop_gput:Nnn \g_ra_arrets_prop {#1/juridiction} {#2}
    \prop_gput:Nnn \g_ra_arrets_prop {#1/date} {#3}
    \prop_gput:Nnn \g_ra_arrets_prop {#1/numero} {#4}
    \prop_gput:NnV \g_ra_arrets_prop {#1/titre} \l_ra_arret_titre_tl
    \prop_gput:NnV \g_ra_arrets_prop {#1/ecli} \l_ra_arret_ecli_tl
    \tl_if_empty:NTF \l_ra_arret_url_tl
     {
       \tl_if_empty:NF \l_ra_arret_id_tl
        {
          \tl_set:Nx \l_ra_arret_url_tl
            {https\char_generate:nn {58}{12}//www.legifrance.gouv.fr/\l_ra_arret_fonds_tl/id/\l_ra_arret_id_tl}
        }
     }{}
    \prop_gput:NnV \g_ra_arrets_prop {#1/url} \l_ra_arret_url_tl
   }
 }
\NewDocumentCommand{\DeclarerArret}{mmmm O{}}
 {\ra_arret_enregistrer:nnnnn {#1}{#2}{#3}{#4}{#5}}
\cs_new:Npn \ra_arret_valeur:nn #1#2
 {\prop_item:Nn \g_ra_arrets_prop {#1/#2}}
\cs_new_protected:Npn \ra_arret_reference:nn #1#2
 {
  \ra_arret_valeur:nn {#1}{juridiction},~
  \ra_arret_valeur:nn {#1}{date},~
  \prop_get:NnN \g_ra_arrets_prop {#1/titre} \l_ra_arret_titre_tl
  \tl_if_empty:NF \l_ra_arret_titre_tl
    {\emph{\tl_use:N \l_ra_arret_titre_tl},~}
  \no~\ra_arret_lien:nnn {#1}{\ra_arret_valeur:nn {#1}{numero}}{#2}
 }
\cs_new_protected:Npn \ra_arret_lien:nnn #1#2#3
 {
  \tl_set:Nx \l_ra_arret_destination_tl {\ra_arret_valeur:nn {#1}{url}}
  \bool_if:nTF {#3 && \g_ra_arrets_liens_bool}
   {
    \tl_if_empty:NTF \l_ra_arret_destination_tl
     {#2}
     {{\color{LienJurisprudence}%
        \exp_args:NV \href \l_ra_arret_destination_tl {#2}}}
   }
   {#2}
 }
\cs_new_protected:Npn \ra_arret_afficher:nnnn #1#2#3#4
 {
  \prop_if_in:NnTF \g_ra_arrets_prop {#1/juridiction}
   {
    \str_if_eq:nnT {#4}{parenthese} {\textup{(}}
    \ra_arret_reference:nn {#1}{#3}
    \tl_if_blank:nF {#2} {,~#2}
    \str_if_eq:nnT {#4}{parenthese} {\textup{)}}
   }
   {\ClassError{recoursadmin}{Arret~`#1'~non~declare}{Ajouter~sa~reference~dans~jurisprudence.tex.}}
 }
\NewDocumentCommand{\Arret}{s O{} m}
 {\IfBooleanTF{#1}{\ra_arret_afficher:nnnn {#3}{#2}{\c_false_bool}{hors}}{\ra_arret_afficher:nnnn {#3}{#2}{\c_true_bool}{hors}}}
\NewDocumentCommand{\ArretParenthese}{s O{} m}
 {\IfBooleanTF{#1}{\ra_arret_afficher:nnnn {#3}{#2}{\c_false_bool}{parenthese}}{\ra_arret_afficher:nnnn {#3}{#2}{\c_true_bool}{parenthese}}}
\NewDocumentCommand{\FicheArret}{m}
 {\ra_arret_afficher:nnnn {#1}{}{\c_true_bool}{hors}%
  \prop_get:NnNT \g_ra_arrets_prop {#1/ecli} \l_ra_arret_ecli_tl
    {\tl_if_empty:NF \l_ra_arret_ecli_tl
       {\space (ECLI :~\tl_use:N \l_ra_arret_ecli_tl)}}}
\ExplSyntaxOff
\endinput
