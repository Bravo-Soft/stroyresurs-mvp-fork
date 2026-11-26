"""Просмотр эффективного профиля и выбранного шаблона без сети и записи."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path

from bs4 import BeautifulSoup
from .resolver import ProfileResolver
from .format_v2 import select_page, ProfileRuleError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--profiles',required=True)
    parser.add_argument('--html')
    parser.add_argument('--page-type',choices=['product','company','distributor'],default='product')
    args=parser.parse_args()
    resolver=ProfileResolver(args.profiles)
    try:
        profile=resolver.resolve(args.url)
    except ProfileRuleError as exc:
        print(json.dumps({'error':str(exc),'load_errors':resolver.load_errors},ensure_ascii=False,indent=2));return 2
    if resolver.load_errors:
        print(json.dumps({'errors':resolver.load_errors},ensure_ascii=False,indent=2));return 2
    if profile.is_default():
        print(json.dumps({'error':'Профиль не найден'},ensure_ascii=False));return 2
    if args.html:
        try:
            profile=select_page(profile,args.url,'' if args.page_type=='product' else args.page_type,
                                BeautifulSoup(Path(args.html).read_text(),'html.parser'))
        except ProfileRuleError as exc:
            print(json.dumps({'error':str(exc)},ensure_ascii=False,indent=2));return 2
    print(json.dumps(asdict(profile),ensure_ascii=False,indent=2,default=str))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
