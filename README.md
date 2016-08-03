# PetName

This utility will generate "pet names", consisting of a random combination of an adverb, adjective, and proper name.  These are useful for unique hostnames, for instance.

As such, PetName tries to follow the tenets of Zooko's triangle.  Names are:

 - Human meaningful
 - Decentralized
 - Secure

Furthermore, PetName words are:

 - 3 syllables or less
 - 8 characters or less
 - Recognized by dict(1) as a word
 - A regularly used word according to the SCOWL database
 - Adverbs end in "-ly", for consistency

A 1-word PetName consists of one random name.  A 2-word Petname consists of a random adjective and a random name.  A 3-word (or more than 3 word) PetName consists of random adverb(s) and an adjective and a name.

## Command Line Usage

Command line help:

    usage: petname [-w|--words INT] [-s|--separator STR] [-l|--letters INT]

    optional arguments:
        -w|--words            number of words in the name, default is 2
        -l|--letters          maximum number of letters in each word, default is 6
        -s|--separator        string used to separate name words, default is '-'
        -d|--dir              directory containing adverbs.txt, adjectives.txt, names.txt, default is \fI/usr/share/petname/\fP

## Command Line Examples

    $ petname
    scary-mara

    $ petname --words 1
    marco

    $ petname --words 3
    first-wary-jenae

    $ petname --words 4
    coaly-filly-rare-mark

    $ petname --separator ":"
    leery:jolie

    $ petname --separator "" --words 3
    follypilyjaney

## Golang Examples
```golang
package main

import (
        "flag"
        "fmt"
        "github.com/dustinkirkland/golang-petname"
)

var (
        words = flag.Int("words", 2, "The number of words in the pet name")
        separator = flag.String("separator", "-", "The separator between words in the pet name")
)

func main() {
        flag.Parse()
        fmt.Println(petname.Generate(*words, *separator))
}
```

## Python Examples

See: https://pypi.golang.org/pypi/petname

    $ pip install petname
    $ sudo apt-get install golang-petname

```golang
import argparse
import petname

parser = argparse.ArgumentParser(description='Generate human readable random names')
parser.add_argument('-w', '--words', help='Number of words in name, default=2', default=2)
parser.add_argument('-s', '--separator', help='Separator between words, default="-"', default="-")
parser.options = parser.parse_args()

print petname.Generate(int(parser.options.words), parser.options.separator)
```

## Credits

This project is authored and maintained by Dustin Kirkland.

